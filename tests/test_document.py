"""The Companion document is what runs.

The property under test is the one whose absence was the whole problem: on hardware the framework
executed a fixed hand-written pipeline while `export_flowgraph` wrote a `.grc` derived from a spec,
so the file an operator could open depicted a graph that had never run.
"""
import pytest

pytest.importorskip("gnuradio")
pytestmark = pytest.mark.gnuradio

from gr_autopilot.flowgraph import document as doc_mod  # noqa: E402
from gr_autopilot.flowgraph.platform import DocumentRejected  # noqa: E402


def _tone_doc(ident="probe_fg", n=4096, freq="1000"):
    """A minimal but genuinely runnable document: tone -> head -> vector sink.

    `blocks_head` is the measurement window — it bounds the capture without killing the graph,
    which is what a bounded trial needs.
    """
    return {
        "metadata": {"file_format": 1, "grc_version": "3.10"},
        "options": {"parameters": {"id": ident, "generate_options": "no_gui",
                                   "output_language": "python", "run": "True"},
                    "states": {"coordinate": [8, 8]}},
        "blocks": [
            {"name": "src", "id": "analog_sig_source_x",
             "parameters": {"type": "complex", "samp_rate": "32000",
                            "waveform": "analog.GR_COS_WAVE", "freq": freq, "amp": "1",
                            "offset": "0", "phase": "0"},
             "states": {"coordinate": [100, 100]}},
            {"name": "hd", "id": "blocks_head",
             "parameters": {"type": "complex", "num_items": str(n), "vlen": "1"},
             "states": {"coordinate": [300, 100]}},
            {"name": "snk", "id": "blocks_vector_sink_x",
             "parameters": {"type": "complex", "vlen": "1", "reserve_items": "1024"},
             "states": {"coordinate": [500, 100]}},
        ],
        "connections": [["src", "0", "hd", "0"], ["hd", "0", "snk", "0"]],
    }


def test_an_authored_document_compiles_and_actually_runs(tmp_path):
    cls = doc_mod.compile_document(_tone_doc(), tmp_path)
    tb = cls()
    tb.start()
    tb.wait()
    data = tb.snk.data()
    assert len(data) == 4096, "the document's own head block should bound the capture"
    assert abs(abs(data[0]) - 1.0) < 1e-6


def test_the_file_on_disk_is_the_graph_that_ran(tmp_path):
    """What an operator opens in Companion must be what produced the measurement."""
    d = _tone_doc(freq="2500")
    cls = doc_mod.compile_document(d, tmp_path)
    written = list(tmp_path.rglob("*.grc"))
    assert len(written) == 1
    reloaded = doc_mod.load(written[0])
    assert doc_mod.document_hash(reloaded) == doc_mod.document_hash(d)
    # and the reloaded file compiles to the same class
    assert doc_mod.compile_document(reloaded, tmp_path) is cls


def test_compilation_is_cached_by_content_not_by_name(tmp_path):
    a = doc_mod.compile_document(_tone_doc(freq="1000"), tmp_path)
    again = doc_mod.compile_document(_tone_doc(freq="1000"), tmp_path)
    assert again is a, "identical documents should not be regenerated"
    different = doc_mod.compile_document(_tone_doc(freq="3000"), tmp_path)
    assert different is not a, "a changed parameter is a different graph"


def test_layout_changes_are_not_graph_changes(tmp_path):
    """Moving a block in the editor must not create a second stored artifact."""
    a = _tone_doc()
    b = _tone_doc()
    b["blocks"][0]["states"]["coordinate"] = [999, 777]
    b["metadata"]["grc_version"] = "3.10.1.1"
    assert doc_mod.document_hash(a) == doc_mod.document_hash(b)


def test_two_documents_sharing_an_id_do_not_shadow_each_other(tmp_path):
    """Both are legitimately named the same; importing under that name would give the second the
    first's graph."""
    a = doc_mod.compile_document(_tone_doc(ident="same", freq="1000"), tmp_path)
    b = doc_mod.compile_document(_tone_doc(ident="same", freq="7000"), tmp_path)
    assert a is not b
    ta, tb_ = a(), b()
    ta.start(); ta.wait()
    tb_.start(); tb_.wait()
    assert len(ta.snk.data()) == len(tb_.snk.data()) == 4096


def test_an_id_that_is_not_an_identifier_is_refused(tmp_path):
    d = _tone_doc()
    d["options"]["parameters"]["id"] = "not a class name"
    with pytest.raises(DocumentRejected, match="identifier"):
        doc_mod.compile_document(d, tmp_path)


def test_a_denied_block_never_reaches_the_compiler(tmp_path):
    d = _tone_doc()
    d["blocks"].append({"name": "evil", "id": "epy_block",
                        "parameters": {"_source_code": "raise SystemExit"},
                        "states": {"coordinate": [700, 100]}})
    with pytest.raises(DocumentRejected, match="arbitrary Python"):
        doc_mod.compile_document(d, tmp_path)


def test_message_ports_survive_as_names(tmp_path):
    """Stream ports are numeric, message ports are named. Coercing ports to int is what breaks a
    renderer or a graph walk on the first packet-radio graph."""
    d = _tone_doc()
    d["connections"].append(["src", "msg_out", "snk", "msg_in"])
    conns = doc_mod.connections(d)
    assert ("src", "msg_out", "snk", "msg_in") in conns
    assert all(isinstance(p, str) for _, p, _, _ in conns)
