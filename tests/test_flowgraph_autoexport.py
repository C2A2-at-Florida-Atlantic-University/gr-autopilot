"""Every structure that runs leaves a Companion file behind, without the agent having to ask.

If a file appeared only when the agent remembered to call ``export_flowgraph``, a full
modulation ladder -- 25 structures, over 200 graded runs -- could leave the portal's Flowgraphs
page empty, because nothing in the task asks the agent to export. The run itself exports.

Four claims. The file appears the first time a structure runs, recorded against that iteration.
Running it again adds nothing. An existing file is never overwritten: a different graph under a
name already taken, or an operator's hand edit, gets a content-addressed file beside it. And a
failed export never fails the measurement it follows.
"""
import json
import re
import urllib.request
from pathlib import Path

from gr_autopilot.telemetry.server import serve
from gr_autopilot.tools.service import AutopilotService

BPSK = {"structure_id": "bpsk_link", "modulation": "bpsk",
        "tx_chain": ["bpsk_mod", "rrc_pulse_shape"],
        "rx_chain": ["agc", "rrc_matched_filter", "symbol_sync", "costas_carrier", "bpsk_demod"]}


def _exports(svc):
    return svc.ledger.store.artifacts(svc.ledger.experiment_id, kind="flowgraph.grc")


def _grc_names(svc):
    return sorted(p.name for p in svc.artifacts_dir.glob("*.grc"))


def test_a_structure_that_runs_is_exported_without_being_asked(tmp_path):
    svc = AutopilotService(runs_dir=tmp_path)
    svc.build_flowgraph(BPSK)
    assert not (svc.artifacts_dir / "bpsk_link.grc").exists()     # building alone writes nothing
    svc.run_flowgraph(n_bits=2000)

    f = svc.artifacts_dir / "bpsk_link.grc"
    assert f.is_file() and "digital_chunks_to_symbols_xx" in f.read_text()
    [row] = _exports(svc)
    assert row["iteration_seq"] == svc.ledger.next_iteration() - 1   # the run that produced it
    assert row["meta"]["structure_id"] == "bpsk_link" and row["meta"]["automatic"] is True


def test_running_the_same_graph_again_adds_nothing(tmp_path):
    svc = AutopilotService(runs_dir=tmp_path)
    svc.build_flowgraph(BPSK)
    for _ in range(3):
        svc.run_flowgraph(n_bits=2000)
    svc.build_flowgraph(BPSK)                 # an identical rebuild is the same graph
    svc.run_flowgraph(n_bits=2000)
    assert _grc_names(svc) == ["bpsk_link.grc"]
    assert len(_exports(svc)) == 1


def test_a_different_graph_under_a_taken_name_is_written_beside_it(tmp_path):
    svc = AutopilotService(runs_dir=tmp_path)
    svc.build_flowgraph(BPSK)
    svc.run_flowgraph(n_bits=2000)
    first = (svc.artifacts_dir / "bpsk_link.grc").read_text()

    svc.build_flowgraph({**BPSK, "pulse_shape": {"type": "rrc", "sps": 4, "rolloff": 0.5}})
    svc.run_flowgraph(n_bits=2000)

    names = _grc_names(svc)
    assert len(names) == 2 and "bpsk_link.grc" in names
    [sibling] = [n for n in names if n != "bpsk_link.grc"]
    assert re.fullmatch(r"bpsk_link-[0-9a-f]{8}\.grc", sibling)
    assert (svc.artifacts_dir / "bpsk_link.grc").read_text() == first    # untouched
    assert "0.5" in (svc.artifacts_dir / sibling).read_text()
    assert [r["iteration_seq"] for r in _exports(svc)] == [1, 2]


def test_an_operator_edit_is_never_overwritten(tmp_path):
    svc = AutopilotService(runs_dir=tmp_path)
    edited = "# hand-edited in gnuradio-companion, waiting to be imported\n"
    svc.artifacts_dir.mkdir(parents=True)
    (svc.artifacts_dir / "bpsk_link.grc").write_text(edited)

    svc.build_flowgraph(BPSK)
    svc.run_flowgraph(n_bits=2000)
    assert (svc.artifacts_dir / "bpsk_link.grc").read_text() == edited
    assert len(list(svc.artifacts_dir.glob("bpsk_link-*.grc"))) == 1


def test_a_structure_name_cannot_write_outside_the_flowgraphs_directory(tmp_path):
    svc = AutopilotService(runs_dir=tmp_path)
    svc.build_flowgraph({**BPSK, "structure_id": "../../escaped"})
    svc.run_flowgraph(n_bits=2000)
    written = list(tmp_path.rglob("*.grc"))
    assert written and all(p.parent == svc.artifacts_dir for p in written)
    # The explicit export's default file name is made safe the same way.
    out = svc.export_flowgraph()
    assert Path(out["path"]).parent == svc.artifacts_dir.resolve()


def test_a_failed_export_does_not_fail_the_run(tmp_path, monkeypatch):
    import gr_autopilot.flowgraph.grc_export as grc_export

    def refuse(*_a, **_k):
        raise OSError("no space left on device")

    monkeypatch.setattr(grc_export, "render_grc", refuse)
    svc = AutopilotService(runs_dir=tmp_path)
    svc.build_flowgraph(BPSK)
    out = svc.run_flowgraph(n_bits=2000)
    assert out["ran"] is True and len(svc.ledger.read()) == 1     # the measurement stands
    assert _exports(svc) == []


def test_the_portal_lists_and_serves_the_automatic_export(tmp_path):
    svc = AutopilotService(runs_dir=tmp_path)
    svc.build_flowgraph(BPSK)
    svc.run_flowgraph(n_bits=2000)
    httpd = serve(lambda: svc.ledger_db_path, lambda: svc.snapshot_path, port=8183,
                  background=True, artifacts_dir=lambda: svc.artifacts_dir, experiments=svc)
    base = "http://127.0.0.1:8183"
    try:
        listing = json.loads(urllib.request.urlopen(base + "/flowgraphs", timeout=5).read())
        assert listing["flowgraphs"] == ["bpsk_link.grc"]
        body = urllib.request.urlopen(base + "/flowgraphs/bpsk_link.grc", timeout=5).read()
        assert body.decode() == (svc.artifacts_dir / "bpsk_link.grc").read_text()
    finally:
        httpd.shutdown()
