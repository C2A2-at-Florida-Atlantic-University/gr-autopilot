"""Editing an exported flowgraph and running the edited version.

Export alone makes a graph inspectable. This closes the loop: a person opens the file in GNU
Radio Companion, changes it, hands it back, and the experiment runs THEIR graph. The properties
worth defending are that the edit genuinely reaches the measurement, that a graph the system
cannot run is refused by name rather than quietly reduced to one it can, and that a result
produced from a hand-edited graph is distinguishable afterwards from one the agent proposed.
"""
import pathlib
import tempfile

import pytest
import yaml

from gr_autopilot.flowgraph.builder import compile_spec
from gr_autopilot.flowgraph.grc_export import write_grc
from gr_autopilot.flowgraph.grc_import import UnsupportedFlowgraph, load_grc
from gr_autopilot.flowgraph.spec import FlowgraphSpec
from gr_autopilot.tools import (AutopilotService, InProcessClient, MCPToolError, StdioMCPServer)

TX = ["qpsk_mod", "rrc_pulse_shape"]
RX = ["rrc_matched_filter", "symbol_sync", "qpsk_demod"]


def _spec(**kw):
    base = dict(structure_id="qpsk_link", modulation="qpsk", tx_chain=list(TX), rx_chain=list(RX),
                pulse_shape={"sps": 4, "rolloff": 0.35})
    base.update(kw)
    return FlowgraphSpec(**base)


def _edit(path, fn):
    """Apply ``fn`` to the parsed document and write it back, as an editor would."""
    doc = yaml.safe_load(pathlib.Path(path).read_text())
    fn(doc)
    out = pathlib.Path(path).with_name("edited.grc")
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
    return out


# -- the round trip ----------------------------------------------------------

def test_an_unedited_export_reimports_to_the_same_chain(tmp_path):
    original = _spec()
    back = load_grc(write_grc(original, tmp_path / "a.grc"))
    assert list(back.spec.tx_chain) == TX
    assert list(back.spec.rx_chain) == RX
    assert back.spec.modulation == "qpsk"


@pytest.mark.parametrize("modulation,tx,rx", [
    ("bpsk", ["bpsk_mod", "rrc_pulse_shape"], ["rrc_matched_filter", "symbol_sync", "bpsk_demod"]),
    ("16qam", ["qam16_mod", "rrc_pulse_shape"],
     ["rrc_matched_filter", "agc", "symbol_sync", "qam16_demod"]),
])
def test_every_modulation_survives_the_round_trip(tmp_path, modulation, tx, rx):
    spec = _spec(modulation=modulation, tx_chain=tx, rx_chain=rx, structure_id=f"{modulation}_link")
    back = load_grc(write_grc(spec, tmp_path / f"{modulation}.grc"))
    assert back.spec.modulation == modulation
    assert list(back.spec.rx_chain) == rx


def test_the_constellation_is_recovered_from_the_symbol_table(tmp_path):
    """Modulation is not written as a label anywhere; it is inferred from how many points the
    mapper carries, which is what a person editing the table would change."""
    back = load_grc(write_grc(_spec(modulation="16qam", tx_chain=["qam16_mod", "rrc_pulse_shape"],
                                    rx_chain=["rrc_matched_filter", "symbol_sync", "qam16_demod"]),
                              tmp_path / "q.grc"))
    assert back.spec.modulation == "16qam"


# -- edits reach the measurement ---------------------------------------------

def test_editing_the_excess_bandwidth_is_read_back(tmp_path):
    path = write_grc(_spec(), tmp_path / "a.grc")

    def widen(doc):
        for b in doc["blocks"]:
            if "taps" in b["parameters"]:
                b["parameters"]["taps"] = b["parameters"]["taps"].replace("0.35", "0.5")

    back = load_grc(_edit(path, widen))
    assert back.overrides["rolloff"] == 0.5
    assert back.spec.pulse_shape["rolloff"] == 0.5


def test_editing_the_timing_loop_bandwidth_reaches_the_link_parameters(tmp_path):
    """The synchronizer's loop bandwidth had no path from the specification into a run at all
    until this existed -- ``sync`` was carried and never read."""
    path = write_grc(_spec(), tmp_path / "a.grc")

    def widen(doc):
        block = next(b for b in doc["blocks"] if b["name"].startswith("symbol_sync"))
        block["parameters"]["loop_bw"] = "0.5"

    back = load_grc(_edit(path, widen))
    assert back.spec.sync["loop_bw"] == 0.5
    assert compile_spec(back.spec)["timing_loop_bw"] == 0.5


def test_adding_a_block_in_the_editor_adds_it_to_the_chain(tmp_path):
    path = write_grc(_spec(), tmp_path / "a.grc")

    def add_costas(doc):
        sync = next(b for b in doc["blocks"] if b["name"].startswith("symbol_sync"))
        doc["blocks"].append({
            "name": "costas_9", "id": "digital_costas_loop_cc",
            "parameters": {"affinity": "", "alias": "", "comment": "", "maxoutbuf": "0",
                           "minoutbuf": "0", "w": "0.0628", "order": "4", "use_snr": "False"},
            "states": {"coordinate": [900, 40], "rotation": 0, "state": "enabled"}})
        doc["connections"] = [c for c in doc["connections"]
                              if not (c[0] == sync["name"] and c[2] == "recovered_symbols")]
        doc["connections"] += [[sync["name"], "0", "costas_9", "0"],
                               ["costas_9", "0", "recovered_symbols", "0"]]

    back = load_grc(_edit(path, add_costas))
    assert list(back.spec.rx_chain) == ["rrc_matched_filter", "symbol_sync", "costas_carrier",
                                        "qpsk_demod"]


def test_removing_a_block_in_the_editor_removes_it_from_the_chain(tmp_path):
    path = write_grc(_spec(), tmp_path / "a.grc")

    def drop_matched_filter(doc):
        mf = next(b for b in doc["blocks"] if b["name"].startswith("matched_filter"))
        upstream = next(c for c in doc["connections"] if c[2] == mf["name"])
        downstream = next(c for c in doc["connections"] if c[0] == mf["name"])
        doc["blocks"] = [b for b in doc["blocks"] if b["name"] != mf["name"]]
        doc["connections"] = [c for c in doc["connections"]
                              if mf["name"] not in (c[0], c[2])]
        doc["connections"].append([upstream[0], "0", downstream[2], "0"])

    back = load_grc(_edit(path, drop_matched_filter))
    assert "rrc_matched_filter" not in back.spec.rx_chain


# -- refusing what it cannot run ---------------------------------------------

def test_an_unknown_block_is_refused_by_name(tmp_path):
    """Silently dropping a block the operator deliberately added would run something other than
    what they asked for, and then report the result as theirs."""
    path = write_grc(_spec(), tmp_path / "a.grc")

    def add_unknown(doc):
        sync = next(b for b in doc["blocks"] if b["name"].startswith("symbol_sync"))
        doc["blocks"].append({"name": "mystery_1", "id": "some_oot_module_block",
                              "parameters": {"comment": ""},
                              "states": {"coordinate": [900, 40], "rotation": 0,
                                         "state": "enabled"}})
        doc["connections"] = [c for c in doc["connections"]
                              if not (c[0] == sync["name"] and c[2] == "recovered_symbols")]
        doc["connections"] += [[sync["name"], "0", "mystery_1", "0"],
                               ["mystery_1", "0", "recovered_symbols", "0"]]

    with pytest.raises(UnsupportedFlowgraph, match="mystery_1"):
        load_grc(_edit(path, add_unknown))


def test_a_file_that_is_not_a_flowgraph_is_rejected(tmp_path):
    bad = tmp_path / "notes.grc"
    bad.write_text("just some text, not a flowgraph\n")
    with pytest.raises(UnsupportedFlowgraph):
        load_grc(bad)


def test_a_graph_with_no_modulator_is_rejected(tmp_path):
    path = write_grc(_spec(), tmp_path / "a.grc")

    def drop_mod(doc):
        mod = next(b for b in doc["blocks"] if b["name"].startswith("mod_"))
        doc["blocks"] = [b for b in doc["blocks"] if b["name"] != mod["name"]]
        doc["connections"] = [c for c in doc["connections"] if mod["name"] not in (c[0], c[2])]
        doc["connections"].append(["payload_source", "0", "pulse_shape_1", "0"])

    with pytest.raises(UnsupportedFlowgraph, match="modulation"):
        load_grc(_edit(path, drop_mod))


# -- provenance --------------------------------------------------------------

def test_the_file_hash_changes_when_the_file_does(tmp_path):
    path = write_grc(_spec(), tmp_path / "a.grc")
    before = load_grc(path).sha256
    after = load_grc(_edit(path, lambda d: d["blocks"].__setitem__(
        0, {**d["blocks"][0], "parameters": {**d["blocks"][0]["parameters"], "comment": "mine"}}))
    ).sha256
    assert before != after and len(before) == 64


def test_importing_through_the_tool_surface_records_it_as_hand_edited(tmp_path):
    svc = AutopilotService(ledger_path=tmp_path / "l.jsonl")
    cli = InProcessClient(StdioMCPServer(svc))
    path = write_grc(_spec(), tmp_path / "a.grc")

    out = cli.call("import_flowgraph", path=str(path))
    assert out["structure_id"] == "qpsk_link"
    assert out["tx_chain"] == TX and out["rx_chain"] == RX
    assert len(out["sha256"]) == 64

    rows = svc.ledger.read()
    assert rows[-1]["verdict"] == "imported"
    artifacts = svc.ledger.store.artifacts(svc.ledger.experiment_id, kind="flowgraph.imported")
    assert artifacts and artifacts[0]["meta"]["sha256"] == out["sha256"]


def test_importing_a_graph_that_cannot_run_is_an_actionable_tool_error(tmp_path):
    svc = AutopilotService(ledger_path=tmp_path / "l.jsonl")
    cli = InProcessClient(StdioMCPServer(svc))
    bad = tmp_path / "bad.grc"
    bad.write_text("not a flowgraph\n")
    with pytest.raises(MCPToolError) as caught:
        cli.call("import_flowgraph", path=str(bad))
    assert caught.value.code in ("unsupported_flowgraph", "import_failed")


def test_importing_a_missing_file_says_so(tmp_path):
    svc = AutopilotService(ledger_path=tmp_path / "l.jsonl")
    cli = InProcessClient(StdioMCPServer(svc))
    with pytest.raises(MCPToolError) as caught:
        cli.call("import_flowgraph", path=str(tmp_path / "nope.grc"))
    assert caught.value.code == "not_found"


def test_an_imported_graph_is_what_subsequent_runs_use(tmp_path):
    svc = AutopilotService(ledger_path=tmp_path / "l.jsonl")
    cli = InProcessClient(StdioMCPServer(svc))
    spec16 = _spec(structure_id="qam16_link", modulation="16qam",
                   tx_chain=["qam16_mod", "rrc_pulse_shape"],
                   rx_chain=["rrc_matched_filter", "symbol_sync", "qam16_demod"])
    cli.call("import_flowgraph", path=str(write_grc(spec16, tmp_path / "b.grc")))
    assert cli.call("get_status")["structure_id"] == "qam16_link"
    cli.call("run_flowgraph", n_bits=8000)
    assert cli.call("get_metrics")["n_bits"] == 8000
