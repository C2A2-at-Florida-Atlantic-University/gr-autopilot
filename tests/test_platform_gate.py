"""The gate in front of GNU Radio's compiler.

The agent authors a Companion document and GNU Radio compiles it. That is only safe because five
blocks are refused by identity. These tests are the refusal, and one of them is the attack it
exists to stop.
"""
import pathlib

import pytest

pytest.importorskip("gnuradio")
pytestmark = pytest.mark.gnuradio

from gr_autopilot.flowgraph.platform import (  # noqa: E402
    DENIED_BLOCKS,
    DocumentRejected,
    check_document,
    known_block_ids,
    validate_document,
)


def _doc(*blocks, connections=None):
    """A minimal Companion document. `metadata.file_format` is required by GRC's importer."""
    return {
        "metadata": {"file_format": 1, "grc_version": "3.10"},
        "options": {"parameters": {"id": "t", "generate_options": "no_gui",
                                   "output_language": "python", "run": "True"},
                    "states": {"coordinate": [8, 8]}},
        "blocks": [{"name": f"b{i}", "id": b, "parameters": p, "states": {"coordinate": [0, 0]}}
                   for i, (b, p) in enumerate(blocks)],
        "connections": connections or [],
    }


def test_catalogue_plus_denylist_equals_the_compiler():
    """The invariant that makes browsing trustworthy.

    Anything the compiler accepts is either visible in the catalogue or explicitly refused. Before
    this held, the catalogue was silently missing exactly the blocks that break the sandbox: an
    agent could not discover them by browsing, and a document naming one compiled anyway.
    """
    from gr_autopilot.blocks.registry import default_registry

    catalogued = {b["id"] for b in default_registry().list_blocks()}
    invisible = known_block_ids() - catalogued
    assert invisible == set(DENIED_BLOCKS), (
        f"blocks the compiler accepts that are neither catalogued nor denied: "
        f"{sorted(invisible - set(DENIED_BLOCKS))}")


@pytest.mark.parametrize("bid", sorted(DENIED_BLOCKS - {"_dummy"}))
def test_each_denied_block_is_refused_with_a_reason(bid):
    with pytest.raises(DocumentRejected) as e:
        check_document(_doc((bid, {})))
    # The refusal has to tell the agent what to do instead, or it burns a round trip guessing.
    assert len(e.value.reasons) == 1
    assert bid in e.value.reasons[0]
    assert len(e.value.reasons[0]) > 60, "a bare 'denied' is not actionable"


def test_the_virtual_connection_attack_is_refused():
    """The cheat that a demo hits and edge-walking cannot see.

    virtual_source/virtual_sink connect by NAME, so the edge never appears in `connections`. A
    graph that loops the modulator straight into the receiver therefore looks like two unconnected
    fragments to any check that walks declared edges — while producing bit error ratio zero with no
    channel and no radio. Refusing the blocks by identity is what closes it.
    """
    attack = _doc(
        ("blocks_vector_source_b", {"vector": "[0,1]", "repeat": "True"}),
        ("digital_chunks_to_symbols_xx", {}),
        ("virtual_sink", {"stream_id": "cheat"}),      # <- edge leaves the document here
        ("virtual_source", {"stream_id": "cheat"}),    # <- and reappears here, unrecorded
        ("blocks_vector_sink_c", {}),
        connections=[["b0", "0", "b1", "0"], ["b1", "0", "b2", "0"],
                     ["b3", "0", "b4", "0"]],
    )
    with pytest.raises(DocumentRejected) as e:
        check_document(attack)
    assert any("do not appear in the document's connection list" in r for r in e.value.reasons)


def test_arbitrary_python_is_refused_even_though_it_has_stream_ports():
    """epy_block declares stream ports so it can sit mid-chain, which is exactly why an arity-based
    filter would pass it. The deny-list is keyed on identity for this reason."""
    # epy_block is built with have_inputs=True, have_outputs=True
    # (gnuradio/grc/core/blocks/embedded_python.py:80) so that it can sit mid-chain — which is
    # exactly why an arity-based filter would let it through. Hence identity, not arity.
    src = pathlib.Path(
        "/usr/lib/python3/dist-packages/gnuradio/grc/core/blocks/embedded_python.py").read_text()
    assert "have_inputs=True, have_outputs=True" in src, (
        "epy_block no longer declares stream ports — this test's premise moved")
    with pytest.raises(DocumentRejected, match="arbitrary Python"):
        check_document(_doc(("epy_block", {"_source_code": "import os; os.system('id')"})))


def test_headless_only():
    with pytest.raises(DocumentRejected, match="headless"):
        check_document(_doc(("qtgui_time_sink_x", {})))
    bad_options = _doc(("blocks_null_source", {}))
    bad_options["blocks"].append(
        {"name": "opts", "id": "options", "parameters": {"generate_options": "qt_gui"}})
    with pytest.raises(DocumentRejected, match="headless"):
        check_document(bad_options)


def test_unknown_block_is_named_not_swallowed():
    with pytest.raises(DocumentRejected, match="not_a_real_block"):
        check_document(_doc(("not_a_real_block", {})))


def test_all_reasons_are_reported_at_once():
    """One compile per mistake is a round trip per mistake, and the mistakes are usually
    correlated."""
    with pytest.raises(DocumentRejected) as e:
        check_document(_doc(("epy_block", {}), ("import", {}), ("qtgui_time_sink_x", {})))
    assert len(e.value.reasons) == 3


def test_a_legitimate_graph_passes_the_gate_and_gnuradio_validates_it():
    doc = _doc(
        ("blocks_null_source", {"type": "complex", "num_streams": "1"}),
        ("blocks_null_sink", {"type": "complex", "num_streams": "1"}),
        connections=[["b0", "0", "b1", "0"]],
    )
    check_document(doc)
    fg = validate_document(doc)
    assert fg is not None


def test_gnuradio_reports_type_errors_rather_than_us():
    """We do not type-check connections; GRC does, and it names the block. Keeping that division is
    the whole reason the compiler is GNU Radio's."""
    doc = _doc(
        ("blocks_null_source", {"type": "complex", "num_streams": "1"}),
        ("blocks_null_sink", {"type": "float", "num_streams": "1"}),
        connections=[["b0", "0", "b1", "0"]],
    )
    check_document(doc)                       # the gate is happy: both blocks are legitimate
    with pytest.raises(DocumentRejected) as e:
        validate_document(doc)
    assert any("type" in r.lower() for r in e.value.reasons)


def test_out_of_tree_blocks_are_visible_to_the_catalogue():
    """"GNU Radio is the platform" has to include a user's own blocks."""
    from gr_autopilot.blocks.registry import default_registry

    ids = {b["id"] for b in default_registry().list_blocks()}
    oot = {"NTSC_decoder_c", "spectrumDetect_specDetect"} & known_block_ids()
    assert oot <= ids, f"out-of-tree blocks the compiler knows are not browsable: {oot - ids}"
