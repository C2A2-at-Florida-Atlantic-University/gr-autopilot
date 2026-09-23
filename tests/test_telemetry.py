"""Telemetry writer + stdlib server (read-only dashboard backend)."""
import json
import urllib.error
import urllib.request

import numpy as np

from gr_autopilot.ledger import EditLedger, LedgerEntry
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.scoring.metrics import compute_metrics
from gr_autopilot.telemetry import TelemetryWriter
from gr_autopilot.telemetry.server import serve
from gr_autopilot.telemetry.writer import constellation_points, spectrum_db


def _result(es_n0_db=12.0):
    r = NumpySimBackend().run_link(
        LinkParams(modulation="qpsk", n_payload_bits=8000, es_n0_db=es_n0_db))
    return r, compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)


def test_writer_snapshot(tmp_path):
    r, m = _result()
    tw = TelemetryWriter(tmp_path / "t.json")
    snap = tw.write(result=r, metrics=m, target_ber=1e-2,
                    structure={"modcod": "qpsk", "center_freq_hz": 2.4e9},
                    active_loop={"loop": "inner", "detail": "trial 2/3"})
    assert (tmp_path / "t.json").exists()
    assert set(snap) >= {"metrics", "constellation", "spectrum", "active_loop", "structure"}
    assert snap["metrics"]["feasible"] == (m.ber <= 1e-2)
    assert len(snap["constellation"]) > 0 and len(snap["constellation"][0]) == 2
    assert json.loads((tmp_path / "t.json").read_text())["t"] == snap["t"]  # valid JSON on disk


def test_writer_carries_occupancy_when_sensing(tmp_path):
    """A monitor sweep rides in the snapshot as an occupancy list; a plain run omits the key."""
    r, m = _result()
    tw = TelemetryWriter(tmp_path / "t.json")
    plain = tw.write(result=r, metrics=m, target_ber=1e-2, structure={"modcod": "qpsk"})
    assert "occupancy" not in plain  # Stage-1 clean link -> no sensing panel

    occ = [{"center_freq_hz": 2.400e9, "power_db": 32.4, "occupied": True},
           {"center_freq_hz": 2.405e9, "power_db": 0.0, "occupied": False}]
    snap = tw.write(result=r, metrics=m, target_ber=1e-2, structure={"modcod": "qpsk"},
                    occupancy=occ)
    assert [o["occupied"] for o in snap["occupancy"]] == [True, False]
    assert snap["occupancy"][0]["center_freq_hz"] == 2.400e9
    on_disk = json.loads((tmp_path / "t.json").read_text())
    assert on_disk["occupancy"] == snap["occupancy"]  # survives the JSON round-trip


def test_writer_carries_token_stats(tmp_path):
    """The MCP token footprint rides in the snapshot; a plain run omits the key."""
    r, m = _result()
    tw = TelemetryWriter(tmp_path / "t.json")
    assert "tokens" not in tw.write(result=r, metrics=m, target_ber=1e-2, structure={"modcod": "qpsk"})
    tok = {"total": 500, "calls": 10, "attempts": 3, "this_attempt": 20,
           "avg_per_attempt": 167, "per_attempt": [200, 150, 150], "estimated": True}
    snap = tw.write(result=r, metrics=m, target_ber=1e-2, structure={"modcod": "qpsk"}, tokens=tok)
    assert snap["tokens"]["total"] == 500 and snap["tokens"]["per_attempt"] == [200, 150, 150]
    assert json.loads((tmp_path / "t.json").read_text())["tokens"] == tok


def test_writer_carries_control_but_redacts_hidden_snr(tmp_path):
    r, m = _result()
    ctl = {"running": True, "target_ber": 1e-2, "es_n0_db": 12.0, "jammer": False}
    snap = TelemetryWriter(tmp_path / "t.json").write(
        result=r, metrics=m, target_ber=1e-2, structure={"modcod": "qpsk"}, control=ctl)
    # agent-legal display state is carried, but the operator's hidden channel setpoint is redacted
    # from the network snapshot (it must not reach GET /data)
    assert "es_n0_db" not in snap["control"]
    assert snap["control"] == {"running": True, "target_ber": 1e-2, "jammer": False}


def test_control_state_validates_and_clamps():
    from gr_autopilot.telemetry.server import ControlState
    cs = ControlState()
    assert cs.get() == {"running": True, "target_ber": 1e-2, "es_n0_db": None, "jammer": False}
    st = cs.apply({"es_n0_db": 999, "target_ber": 0.0, "jammer": True, "bogus": "x"})
    assert st["es_n0_db"] == 40.0 and st["target_ber"] == 1e-6   # clamped to the safe range
    assert st["jammer"] is True and "bogus" not in st            # unknown keys dropped
    cs.apply({"reset": True})
    assert "reset" not in cs.get()                                # one-shot, not public state
    assert cs.take_reset() is True and cs.take_reset() is False   # consumed once


def test_server_control_channel(tmp_path):
    from gr_autopilot.telemetry.server import ControlState, serve
    cs = ControlState()
    httpd = serve(None, None, port=8138, background=True, control=cs)
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8138/control", method="POST",
            data=json.dumps({"running": False, "es_n0_db": 9}).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert resp["running"] is False and resp["es_n0_db"] == 9.0
        assert cs.get()["running"] is False                       # the driving loop would see it
    finally:
        httpd.shutdown()


def test_server_read_only_when_no_control():
    from gr_autopilot.telemetry.server import serve
    httpd = serve(None, None, port=8139, background=True)          # no control -> read-only
    try:
        req = urllib.request.Request("http://127.0.0.1:8139/control", method="POST", data=b"{}")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("control POST must 404 on a read-only server")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        httpd.shutdown()


def test_downsample_helpers():
    pts = constellation_points(np.arange(1000) + 1j * np.arange(1000), n=100)
    assert len(pts) == 100
    sp = spectrum_db(np.random.default_rng(0).standard_normal(2048) + 0j, n=64)
    assert len(sp["freqs"]) == 64 and max(sp["psd_db"]) <= 0.01  # normalized to 0 dB peak


def test_server_serves_page_and_data(tmp_path):
    led = EditLedger(tmp_path / "l.jsonl")
    led.append(LedgerEntry(iteration=1, structure_id="qpsk", edit_description="x",
                           loop="outer", metrics={"BER": 1e-3}))
    r, m = _result()
    TelemetryWriter(tmp_path / "s.json").write(
        result=r, metrics=m, target_ber=1e-2, structure={"modcod": "qpsk"})
    httpd = serve(str(tmp_path / "l.jsonl"), str(tmp_path / "s.json"), port=8137, background=True)
    try:
        page = urllib.request.urlopen("http://127.0.0.1:8137/", timeout=5).read().decode()
        assert "gr-autopilot" in page  # app shell (React dist or the stdlib fallback page)
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8137/data", timeout=5).read())
        assert len(data["ledger"]) == 1
        assert data["snapshot"]["metrics"]["target_ber"] == 1e-2
    finally:
        httpd.shutdown()


# -- liveness: the dashboard must show the CURRENT state of the MCP loop, not the last one -------
# The snapshot file is a last-value store that outlives the run that wrote it. Without an age, a
# stopped mission is indistinguishable from a live one and the page keeps rendering its final
# frame under a green LIVE badge. GET /data therefore ages every snapshot it serves.

def test_snapshot_carries_wall_clock_ts(tmp_path):
    import time as _time
    r, m = _result()
    before = _time.time()
    snap = TelemetryWriter(tmp_path / "t.json").write(
        result=r, metrics=m, target_ber=1e-2, structure={"modcod": "qpsk"})
    assert before <= snap["ts"] <= _time.time()
    assert json.loads((tmp_path / "t.json").read_text())["ts"] == snap["ts"]


def test_data_ages_a_fresh_snapshot_as_live(tmp_path):
    r, m = _result()
    TelemetryWriter(tmp_path / "s.json").write(
        result=r, metrics=m, target_ber=1e-2, structure={"modcod": "qpsk"})
    httpd = serve(None, str(tmp_path / "s.json"), port=8141, background=True)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8141/data", timeout=5).read())
        assert data["age_s"] is not None and data["age_s"] < 5.0
    finally:
        httpd.shutdown()


def test_data_ages_a_stale_snapshot(tmp_path):
    """A snapshot from a run that stopped an hour ago must report its true age, so the page can
    fall back to IDLE instead of presenting it as the live state of the radio."""
    r, m = _result()
    snap_path = tmp_path / "s.json"
    TelemetryWriter(snap_path).write(result=r, metrics=m, target_ber=1e-2,
                                     structure={"modcod": "qpsk"})
    snap = json.loads(snap_path.read_text())
    snap["ts"] = snap["ts"] - 3600.0                       # as if written an hour ago
    snap_path.write_text(json.dumps(snap))

    httpd = serve(None, str(snap_path), port=8142, background=True)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8142/data", timeout=5).read())
        assert data["snapshot"] is not None                 # data still served...
        assert 3500 < data["age_s"] < 3700                  # ...but unmistakably stale
    finally:
        httpd.shutdown()


def test_data_age_is_none_without_a_snapshot(tmp_path):
    httpd = serve(None, str(tmp_path / "missing.json"), port=8143, background=True)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8143/data", timeout=5).read())
        assert data["snapshot"] is None and data["age_s"] is None
    finally:
        httpd.shutdown()


def test_data_age_falls_back_to_mtime_for_a_snapshot_without_ts(tmp_path):
    """Snapshots written before ``ts`` existed still age correctly (file mtime), so an old file on
    disk can never masquerade as live."""
    import os as _os
    import time as _time
    r, m = _result()
    snap_path = tmp_path / "s.json"
    TelemetryWriter(snap_path).write(result=r, metrics=m, target_ber=1e-2,
                                     structure={"modcod": "qpsk"})
    snap = json.loads(snap_path.read_text())
    snap.pop("ts")                                          # legacy snapshot
    snap_path.write_text(json.dumps(snap))
    old = _time.time() - 600.0
    _os.utime(snap_path, (old, old))

    httpd = serve(None, str(snap_path), port=8144, background=True)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8144/data", timeout=5).read())
        assert 550 < data["age_s"] < 700
    finally:
        httpd.shutdown()


def test_data_age_never_negative_under_clock_skew(tmp_path):
    """A future-dated ts (clock skew) must clamp to 0, not read as 'impossibly fresh'."""
    r, m = _result()
    snap_path = tmp_path / "s.json"
    TelemetryWriter(snap_path).write(result=r, metrics=m, target_ber=1e-2,
                                     structure={"modcod": "qpsk"})
    snap = json.loads(snap_path.read_text())
    snap["ts"] = snap["ts"] + 600.0
    snap_path.write_text(json.dumps(snap))

    httpd = serve(None, str(snap_path), port=8145, background=True)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8145/data", timeout=5).read())
        assert data["age_s"] == 0.0
    finally:
        httpd.shutdown()


def test_experiments_endpoint_reports_runs_and_their_artifacts(tmp_path):
    """The portal must be able to answer "what has been run, and what did each run produce" --
    the question the previous flat log could not, because it had no experiment boundaries."""
    from gr_autopilot.ledger.ledger import EditLedger, LedgerEntry

    led = EditLedger(tmp_path / "l.jsonl", experiment="demo-run", goal="hold BER <= 1e-2")
    led.append(LedgerEntry(1, "qpsk", "baseline", metrics={"BER": 3e-3}, verdict="kept"))
    led.append(LedgerEntry(2, "16qam", "climb", metrics={"BER": 2e-2}, verdict="reverted"))
    led.store.add_artifact(led.experiment_id, "flowgraph.grc", tmp_path / "qpsk.grc",
                           iteration_seq=1, meta={"modulation": "qpsk"})

    httpd = serve(str(tmp_path / "l.jsonl"), None, port=8146, background=True)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8146/experiments",
                                                 timeout=5).read())
        assert len(data["experiments"]) == 1
        exp = data["experiments"][0]
        assert exp["name"] == "demo-run" and exp["iterations"] == 2
        assert exp["best_ber"] == 3e-3
        assert exp["artifacts"][0]["meta"]["modulation"] == "qpsk"
    finally:
        httpd.shutdown()


def test_data_endpoint_reads_iterations_from_the_database(tmp_path):
    from gr_autopilot.ledger.ledger import EditLedger, LedgerEntry

    led = EditLedger(tmp_path / "l.jsonl")
    for i in range(1, 4):
        led.append(LedgerEntry(i, "qpsk", f"step{i}", metrics={"BER": 1e-3}))
    httpd = serve(str(tmp_path / "l.jsonl"), None, port=8147, background=True)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8147/data", timeout=5).read())
        assert [r["edit_description"] for r in data["ledger"]] == ["step1", "step2", "step3"]
    finally:
        httpd.shutdown()


def test_devices_endpoint_is_measured_on_each_request(tmp_path):
    """Device liveness must be asked for, not remembered. The whole point is that a radio can
    disappear between one poll and the next, so a value cached at startup would be worse than
    useless -- it would keep asserting a radio is present after it has gone."""
    state = {"alive": True}

    def report():
        return {"devices": {"tx": {"uri": "ip:a", "alive": True, "temp_c": 38.0},
                            "rx": {"uri": "ip:b", "alive": state["alive"],
                                   "reason": None if state["alive"] else "no answer"}},
                "backend": "pluto"}

    httpd = serve(None, None, port=8148, background=True, devices=report)
    try:
        first = json.loads(urllib.request.urlopen("http://127.0.0.1:8148/devices",
                                                  timeout=5).read())
        assert first["devices"]["rx"]["alive"] is True

        state["alive"] = False              # the receiver is unplugged
        second = json.loads(urllib.request.urlopen("http://127.0.0.1:8148/devices",
                                                   timeout=5).read())
        assert second["devices"]["rx"]["alive"] is False
        # ...and the transmitter is untouched: one radio going must not blank a healthy one.
        assert second["devices"]["tx"]["alive"] is True
    finally:
        httpd.shutdown()


def test_devices_endpoint_says_so_when_there_are_no_radios(tmp_path):
    httpd = serve(None, None, port=8149, background=True)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8149/devices",
                                                 timeout=5).read())
        assert data["devices"] == {} and data["backend"] == "simulated"
    finally:
        httpd.shutdown()


def test_a_failing_device_probe_does_not_take_the_page_down(tmp_path):
    def report():
        raise RuntimeError("probe exploded")

    httpd = serve(None, None, port=8150, background=True, devices=report)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8150/devices",
                                                 timeout=5).read())
        assert data["devices"] == {} and "probe exploded" in data["error"]
    finally:
        httpd.shutdown()


# ---- CSRF guard: same-origin is "served by this daemon", wherever it is bound -----------------

def test_cross_origin_accepts_a_page_this_daemon_served_over_the_lan():
    from gr_autopilot.telemetry.server import _cross_origin
    same = {"Origin": "http://10.0.0.222:8080", "Host": "10.0.0.222:8080", "Sec-Fetch-Site": "same-origin"}
    assert _cross_origin(same) is False
    assert _cross_origin({"Origin": "http://localhost:8080", "Host": "10.0.0.222:8080"}) is False
    assert _cross_origin({"Host": "10.0.0.222:8080"}) is False                     # curl: no Origin
    assert _cross_origin({"Origin": "http://evil.example", "Host": "10.0.0.222:8080"}) is True
    assert _cross_origin({"Origin": "http://10.0.0.222:9999", "Host": "10.0.0.222:8080"}) is True
    assert _cross_origin({"Origin": "http://10.0.0.222:8080", "Host": "10.0.0.222:8080",
                          "Sec-Fetch-Site": "cross-site"}) is True
