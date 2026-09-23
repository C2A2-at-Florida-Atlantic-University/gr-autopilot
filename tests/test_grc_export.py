"""Exporting an experiment as a GNU Radio Companion file.

Two claims are being defended. The first is that the file is VALID -- it opens and compiles in
the editor GNU Radio ships. The strongest available check is to run Companion's own compiler over
it, which is what ``test_exported_file_compiles_with_gnu_radios_own_compiler`` does; a test that
merely re-read the YAML would prove nothing about whether a person can open it.

The second, and more important, is that the file is FAITHFUL: every block in it corresponds to a
block the experiment actually instantiates. An export that drew a signal chain the measurement
did not use would attribute results to a structure that never ran, which is worse than exporting
nothing at all.
"""
import shutil
import subprocess

import pytest
import yaml

from gr_autopilot.flowgraph.grc_export import build_grc, write_grc
from gr_autopilot.flowgraph.spec import FlowgraphSpec

TX = ["qpsk_mod", "rrc_pulse_shape"]
RX = ["rrc_matched_filter", "symbol_sync", "costas_carrier", "qpsk_demod"]


def _spec(structure_id="qpsk_link", modulation="qpsk", tx=None, rx=None):
    return FlowgraphSpec(structure_id=structure_id, modulation=modulation,
                         tx_chain=list(tx or TX), rx_chain=list(rx or RX),
                         pulse_shape={"sps": 4, "rolloff": 0.35})


def _block_ids(doc):
    return [b["id"] for b in doc["blocks"]]


def _names(doc):
    return [b["name"] for b in doc["blocks"]]


# -- shape -------------------------------------------------------------------

def test_document_has_the_sections_companion_requires():
    doc = build_grc(_spec())
    assert set(doc) == {"options", "blocks", "connections", "metadata"}
    assert doc["metadata"]["file_format"] == 1
    assert doc["options"]["parameters"]["id"] == "qpsk_link"


def test_every_parameter_is_serialized_as_a_string():
    """Companion stores parameters as strings, including numbers; a bare int fails to load."""
    doc = build_grc(_spec())
    for block in doc["blocks"]:
        for key, value in block["parameters"].items():
            assert isinstance(value, str), f"{block['name']}.{key} is {type(value).__name__}"


def test_the_graph_is_connected_end_to_end():
    doc = build_grc(_spec())
    edges = {(a, c) for a, _b, c, _d in doc["connections"]}
    reachable, frontier = {"payload_source"}, ["payload_source"]
    while frontier:
        node = frontier.pop()
        for src, dst in edges:
            if src == node and dst not in reachable:
                reachable.add(dst)
                frontier.append(dst)
    assert "recovered_symbols" in reachable, "the sink is not reachable from the source"


# -- faithfulness ------------------------------------------------------------

def test_each_agent_placed_skill_appears_as_a_block():
    doc = build_grc(_spec())
    names = " ".join(_names(doc))
    for expected in ("mod_", "pulse_shape_", "matched_filter_", "symbol_sync_", "costas_"):
        assert expected in names, f"{expected} missing from the exported graph"


def test_demodulators_are_not_drawn_because_no_block_runs_them():
    """The symbol decision belongs to the scoring layer. Drawing a demodulator block would
    imply the graph does something it does not."""
    doc = build_grc(_spec())
    assert not any("demod" in n for n in _names(doc))


def test_the_framework_channel_is_present_and_labelled_as_such():
    doc = build_grc(_spec())
    assert "analog_noise_source_x" in _block_ids(doc)
    channel = next(b for b in doc["blocks"] if b["name"] == "channel")
    assert "framework-owned" in channel["parameters"]["comment"]


def test_chain_changes_are_reflected_in_the_exported_graph():
    """The export must track the specification, not a template."""
    full = build_grc(_spec())
    thin = build_grc(_spec(rx=["qpsk_demod"]))
    assert len(full["blocks"]) > len(thin["blocks"])
    assert "digital_symbol_sync_xx" in _block_ids(full)
    assert "digital_symbol_sync_xx" not in _block_ids(thin)


def test_matched_filter_decimation_matches_what_the_backend_does():
    """The exported decimation must equal the executed one, or the file is a different graph."""
    doc = build_grc(_spec())
    mf = next(b for b in doc["blocks"] if b["name"].startswith("matched_filter"))
    assert mf["parameters"]["decim"] == "2"      # 4 samples/symbol in, 2 out
    ss = next(b for b in doc["blocks"] if b["name"].startswith("symbol_sync"))
    assert ss["parameters"]["sps"] == "2.0"      # and the synchronizer is told so


def test_carrier_offset_only_appears_when_there_is_one():
    assert "blocks_rotator_cc" not in _block_ids(build_grc(_spec()))
    assert "blocks_rotator_cc" in _block_ids(
        build_grc(_spec(), freq_offset_hz=2000.0, sample_rate=1e6))


def test_offset_is_expressed_without_the_math_module():
    """Companion evaluates parameters in a namespace without ``math``; using ``math.pi`` there is
    a load error, not a value. Regression guard: this exact mistake broke the first export."""
    doc = build_grc(_spec(), freq_offset_hz=2000.0, sample_rate=1e6)
    rot = next(b for b in doc["blocks"] if b["name"] == "carrier_offset")
    assert "math." not in rot["parameters"]["phase_inc"]
    assert {"samp_rate", "freq_offset_hz"} <= set(_names(doc))


def test_export_notes_flag_the_two_places_the_file_departs_from_the_experiment():
    doc = build_grc(_spec())
    comments = " ".join(b["parameters"].get("comment", "") for b in doc["blocks"])
    assert "EXPORT NOTE" in comments
    assert comments.count("EXPORT NOTE") >= 2      # the payload source and the sink


# -- validity ----------------------------------------------------------------

def test_write_grc_produces_loadable_yaml(tmp_path):
    path = write_grc(_spec(), tmp_path / "out.grc")
    assert path.exists()
    assert yaml.safe_load(path.read_text())["metadata"]["file_format"] == 1


@pytest.mark.skipif(shutil.which("grcc") is None, reason="GNU Radio Companion compiler not found")
@pytest.mark.parametrize("modulation,tx,rx,offset", [
    ("qpsk", TX, RX, 2000.0),
    ("16qam", ["qam16_mod", "rrc_pulse_shape"],
     ["rrc_matched_filter", "agc", "symbol_sync", "qam16_demod"], 0.0),
    ("bpsk", ["bpsk_mod", "rrc_pulse_shape"],
     ["rrc_matched_filter", "symbol_sync", "bpsk_demod"], 0.0),
])
def test_exported_file_compiles_with_gnu_radios_own_compiler(tmp_path, modulation, tx, rx, offset):
    """The real test of validity: hand the file to the compiler GNU Radio ships and require it to
    produce a runnable program."""
    spec = _spec(structure_id=f"{modulation}_link", modulation=modulation, tx=tx, rx=rx)
    path = write_grc(spec, tmp_path / f"{modulation}.grc", freq_offset_hz=offset, sample_rate=1e6)
    proc = subprocess.run(["grcc", str(path), "-o", str(tmp_path)],
                          capture_output=True, text=True, timeout=180)
    combined = proc.stdout + proc.stderr
    assert "Load Error" not in combined and "Compilation error" not in combined, combined[-1500:]
    assert list(tmp_path.glob("*.py")), combined[-1500:]


def test_identifier_is_valid_even_when_the_structure_name_starts_with_a_digit():
    """Companion requires the flowgraph identifier to begin with a letter, and structures here
    are routinely named after a modulation, e.g. "16qam_link"."""
    doc = build_grc(_spec(structure_id="16qam_link", modulation="16qam",
                          tx=["qam16_mod"], rx=["qam16_demod"]))
    flow_id = doc["options"]["parameters"]["id"]
    assert flow_id.isidentifier() and flow_id[0].isalpha()
