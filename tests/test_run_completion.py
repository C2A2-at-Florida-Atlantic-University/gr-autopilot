"""Ending a run is something the agent SAYS, not something the console guesses.

A console that inferred completion from the ledger going quiet would be guessing at something
that is not observable from outside: a ledger that has stopped growing is a finished run and an
agent pausing to read a constellation, diagnose a rung or write an annotation, and on the radios
those pauses routinely outlast any threshold worth setting. The run report would appear in the
MIDDLE of a ladder -- and then again, and again, as each later trial re-armed the same guess. An
interim report is worse than no report: it invites the operator to act on a number the rest of
the run will move.

So completion is a signal rather than an inference. ``finish_experiment`` stamps ``ended_at``; the
console fires on that stamp and on nothing else. Three properties make it usable as a one-shot
trigger, and they are what this file pins down:

  * finishing stamps the experiment, exactly once -- a second call changes nothing;
  * appending anything afterwards CLEARS the stamp, so a run that turns out to have more to
    measure carries on and its true ending raises a new, later report;
  * the stamp reaches the console over the same ``/data`` poll as everything else.

The fourth claim here is about content rather than timing: a report has to be able to say what the
run DID, so the decisions the agent took -- retunes, hop plans, power changes, band sweeps -- are
recorded beside the measurements instead of vanishing.
"""
import json
import urllib.request

import pytest

from gr_autopilot.hardware.topology import load_topology
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.tools.service import AutopilotService, ToolError

BPSK = {"structure_id": "bpsk_link", "modulation": "bpsk",
        "tx_chain": ["bpsk_mod", "rrc_pulse_shape"],
        "rx_chain": ["agc", "rrc_matched_filter", "symbol_sync", "costas_carrier", "bpsk_demod"]}


def _tunable(tmp_path):
    """A service whose backend owns its channel, so retunes and hop plans are callable."""
    be = InterferenceSimBackend(clean_es_n0_db=20.0,
                                interferer=Interferer(center_freq_hz=2.401e9, inr_db=25.0),
                                center_freq_hz=2.400e9)
    return AutopilotService(backend=be, experiment=None, runs_dir=tmp_path)


_BENCH = """
version: 1
name: declared-bench
attestation: {medium: coax, antennas_attached: false, operator: t@example.com, date: 2026-09-07}
devices:
  pluto2: {kind: pluto, role: transmitter, iio_uri: "ip:192.168.2.1", chip: ad9364,
           tuning_range_hz: [70.0e6, 6.0e9], serial: "AAA"}
  pluto3: {kind: pluto, role: receiver, iio_uri: "ip:192.168.3.1", chip: ad9363a,
           tuning_range_hz: [325.0e6, 3.8e9], serial: "BBB"}
path:
  - {from: pluto2.rf_out, attenuator_db: 20, to: pluto3.rf_in}
common:
  demo_center_freq_hz: %s
"""


def _declared_bench(tmp_path, centre_hz):
    """A bench that DECLARES where the link starts, which is the fact the channel column needs."""
    p = tmp_path / "bench.yaml"
    p.write_text(_BENCH % repr(float(centre_hz)))
    return load_topology(p)


def _ran(svc, n=1):
    svc.build_flowgraph(BPSK)
    for _ in range(n):
        svc.run_flowgraph(n_bits=2000)


# ---- the signal -------------------------------------------------------------------------------

def test_a_run_is_not_finished_until_the_agent_says_so(tmp_path):
    """Measuring, pausing and measuring again never ends a run. Only finish_experiment does."""
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    svc.start_experiment("ladder")
    _ran(svc, 3)
    assert svc.current_experiment()["ended_at"] is None
    assert svc.current_experiment()["running"] is True

    out = svc.finish_experiment("settled on BPSK")
    assert out["action"] == "finished" and out["ended_at"] > 0
    assert svc.current_experiment()["ended_at"] == pytest.approx(out["ended_at"])
    assert svc.current_experiment()["running"] is False


def test_finishing_twice_raises_one_report_not_two(tmp_path):
    """The stamp is the trigger, so re-stamping would fire a second popup for the same run."""
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    svc.start_experiment("ladder")
    _ran(svc)

    first = svc.finish_experiment()
    again = svc.finish_experiment()
    assert first["action"] == "finished" and again["action"] == "already_finished"
    assert "ended_at" not in again                     # nothing new to fire on
    assert svc.current_experiment()["ended_at"] == pytest.approx(first["ended_at"])


def test_measuring_after_a_conclusion_reopens_the_run(tmp_path):
    """A run concluded too early is not sealed: it carries on and ends again, later.

    This is what keeps the trigger safe to use. Without it, an agent that concluded and then found
    one more rung worth trying would leave the console showing a report for a run that was still
    going -- the exact failure the signal exists to prevent.
    """
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    svc.start_experiment("ladder")
    _ran(svc)
    first = svc.finish_experiment()["ended_at"]

    svc.run_flowgraph(n_bits=2000)                     # more measuring arrives
    assert svc.current_experiment()["ended_at"] is None
    assert svc.current_experiment()["running"] is True

    second = svc.finish_experiment()
    assert second["action"] == "finished" and second["ended_at"] > first


def test_reopening_a_concluded_experiment_continues_its_history(tmp_path):
    """switch_experiment must not fork a second history inside a concluded experiment's database."""
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    svc.start_experiment("ladder")
    _ran(svc, 2)
    svc.finish_experiment()

    svc.start_experiment("other")
    svc.switch_experiment("ladder")
    assert svc.current_experiment()["iterations"] == 2      # the same two, not a fresh count
    _ran(svc)
    assert len(svc.ledger.read()) == 3


def test_finishing_refuses_without_an_experiment(tmp_path):
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    with pytest.raises(ToolError) as e:
        svc.finish_experiment()
    assert e.value.code == "no_experiment"


# ---- the signal reaches the console -------------------------------------------------------------

def test_the_stamp_reaches_the_dashboard_over_data(tmp_path):
    """The console watches /data. If ended_at does not arrive there, nothing fires."""
    from gr_autopilot.telemetry.server import serve

    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    httpd = serve(lambda: svc.ledger_db_path, lambda: svc.snapshot_path, port=8176,
                  background=True, artifacts_dir=lambda: svc.artifacts_dir, experiments=svc)
    base = "http://127.0.0.1:8176"
    try:
        svc.start_experiment("ladder")
        _ran(svc, 2)
        get = lambda: json.loads(urllib.request.urlopen(base + "/data", timeout=5).read())
        assert get()["experiment"]["ended_at"] is None      # mid-run: no report

        ended = svc.finish_experiment()["ended_at"]
        assert get()["experiment"]["ended_at"] == pytest.approx(ended)

        svc.run_flowgraph(n_bits=2000)                      # reopened
        assert get()["experiment"]["ended_at"] is None
    finally:
        httpd.shutdown()


# ---- what the report is made of -----------------------------------------------------------------

def test_decisions_are_recorded_beside_the_measurements(tmp_path):
    """A retune or a hop plan is half of what a run did, and a table of trials cannot show it.

    Decision rows carry no metrics on purpose: anything pooling error ratios must not find one.
    """
    svc = _tunable(tmp_path)
    svc.start_experiment("avoidance")
    _ran(svc)
    svc.sense_spectrum([2.400e9, 2.401e9, 2.405e9])
    svc.set_center_freq(2.405e9)
    svc.set_hop_plan([2.400e9, 2.401e9, 2.402e9], 500.0)
    svc.clear_hop_plan()

    rows = svc.ledger.read()
    by_verdict = {r["verdict"]: r for r in rows}
    assert {"sensed", "retuned", "hopping", "hopping_cleared"} <= set(by_verdict)
    for v in ("sensed", "retuned", "hopping", "hopping_cleared"):
        assert by_verdict[v]["metrics"] == {}, f"{v} is a decision, not a measurement"
    assert by_verdict["retuned"]["params"]["center_freq_hz"] == 2.405e9
    assert by_verdict["hopping"]["params"]["hop_rate_hz"] == 500.0
    assert len(by_verdict["sensed"]["params"]["occupancy"]) == 3

    # Only the graded run is a trial; the four decisions are not.
    graded = [r for r in rows if "BER" in (r["metrics"] or {})]
    assert len(graded) == 1


def test_a_measurement_records_the_channel_it_was_taken_on(tmp_path):
    """Two identical structures either side of a retune are two different measurements.

    Without the centre frequency on the run row, a frequency-avoidance experiment reads back as a
    column of ``bpsk_link`` trials whose error ratio changes for no stated reason -- the one fact
    that explains the change is in a decision row three lines up, and nothing joins them. The
    console's channel column and the run report both attribute a trial by this field.
    """
    svc = _tunable(tmp_path)
    svc.start_experiment("avoidance")
    svc.set_center_freq(2.405e9)
    _ran(svc)
    svc.set_center_freq(2.402e9)
    _ran(svc)

    graded = [r for r in svc.ledger.read() if "BER" in (r["metrics"] or {})]
    assert [r["params"]["center_freq_hz"] for r in graded] == [2.405e9, 2.402e9]


def test_a_run_records_its_channel_before_the_agent_has_ever_retuned(tmp_path):
    """The channel the bench DECLARED is still a channel the measurement was taken on.

    If attribution read the frequency out of the agent's own RF state, which is empty until the
    agent moves, it would start at the first retune, and a run that met a jammer on the declared
    channel would record no frequency at all: the baseline and the jammed trials would read as the
    same unnamed channel, the first retune would have no origin to print ("- -> 2.380 GHz"), and
    the console could not match a sweep's occupied row against the link's own centre to flag it.
    Every one of those is the same missing fact, and this is where it comes from.
    """
    topo = _declared_bench(tmp_path, 2.370e9)
    be = InterferenceSimBackend(clean_es_n0_db=20.0,
                                interferer=Interferer(center_freq_hz=2.370e9, inr_db=25.0),
                                center_freq_hz=2.370e9)
    svc = AutopilotService(backend=be, experiment=None, runs_dir=tmp_path, topology=topo)
    svc.start_experiment("avoidance")
    _ran(svc)                                   # before any retune at all
    swept = svc.sense_spectrum([2.360e9, 2.370e9, 2.380e9])
    assert any(o["occupied"] for o in swept)
    moved = svc.set_center_freq(2.380e9)
    _ran(svc)

    # The retune knows what it moved OFF, which is what lets a report name the escaped channel.
    assert moved["previous_center_freq_hz"] == 2.370e9
    graded = [r for r in svc.ledger.read() if "BER" in (r["metrics"] or {})]
    assert [r["params"]["center_freq_hz"] for r in graded] == [2.370e9, 2.380e9]
    # The sweep was taken while the link sat on the declared channel, and says so.
    sweep = next(r for r in svc.ledger.read() if r["verdict"] == "sensed")
    assert sweep["params"]["center_freq_hz"] == 2.370e9


def test_a_sweep_records_the_threshold_it_judged_each_channel_against(tmp_path):
    """A margin without its threshold cannot be read.

    +4 dB over the noise floor is occupied on the simulated backend (3 dB) and clear on the
    Plutos (6 dB). A console that flags interference from the sweep has to carry the number the
    backend actually decided with, or it is inventing a threshold of its own -- which is exactly
    the bug the flag had when it was drawn from a fixed level on the PSD trace.
    """
    svc = _tunable(tmp_path)
    svc.start_experiment("avoidance")
    occ = svc.sense_spectrum([2.400e9, 2.401e9, 2.405e9])

    row = next(r for r in svc.ledger.read() if r["verdict"] == "sensed")
    recorded = row["params"]["occupancy"]
    assert [c["center_freq_hz"] for c in recorded] == [2.400e9, 2.401e9, 2.405e9]
    assert all(c["detect_margin_db"] is not None for c in recorded)
    # The row is the sweep, not a summary of it: every field the flag reads back must agree.
    assert [c["occupied"] for c in recorded] == [bool(o["occupied"]) for o in occ]
    assert [c["power_db"] for c in recorded] == [o["power_db"] for o in occ]
    # The jammer at 2.401 GHz stands above its neighbours, which is the whole claim.
    assert recorded[1]["occupied"] and not recorded[2]["occupied"]


def test_a_diagnosis_is_recorded_as_something_the_agent_saw(tmp_path):
    """The agent looking at the constellation is a step in the story, not a private thought.

    A run that degrades, is diagnosed, and is then retuned away from reads back without the middle
    step unless the diagnosis is written down -- and the spur features it carries are the only
    evidence of an interferer available between silent sweeps.
    """
    svc = _tunable(tmp_path)
    svc.start_experiment("avoidance")
    _ran(svc)
    out = svc.diagnose_signal()

    row = next(r for r in svc.ledger.read() if r["verdict"] == "diagnosed")
    assert row["metrics"] == {}                       # a look is not a measurement
    assert row["params"]["fault"] == out["fault"]
    assert row["params"]["modulation"] == "bpsk"
    # A fresh capture invalidates the previous read of it.
    _ran(svc)
    assert svc._last_diagnosis is None


def test_the_conclusion_carries_the_runs_outcome(tmp_path):
    """finish_experiment returns what the run came to, so the agent's report and the console's
    are built from the same numbers rather than from two independent readings of the ledger."""
    svc = _tunable(tmp_path)
    svc.start_experiment("ladder")
    _ran(svc, 2)
    svc.set_center_freq(2.405e9)

    out = svc.finish_experiment("kept BPSK")
    assert out["graded_trials"] == 2
    assert out["structures"] == ["bpsk_link"]
    assert out["current_structure_id"] == "bpsk_link"
    assert out["best_trial"]["structure_id"] == "bpsk_link"
    assert "BER" in out["best_trial"]["metrics"]
    assert [d["verdict"] for d in out["decisions"]] == ["retuned", "conclusion"]
    assert out["decisions"][-1]["what"] == "kept BPSK"
