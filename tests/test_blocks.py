"""Block-registry + connection-validation tests (needs installed GNU Radio block YAMLs)."""
import os

import pytest

from gr_autopilot.blocks import check_connection, resolve_port_dtype
from gr_autopilot.blocks.registry import BlockRegistry

pytestmark = pytest.mark.gnuradio

if not os.path.isdir("/usr/share/gnuradio/grc/blocks"):
    pytest.skip("GRC block YAMLs not installed", allow_module_level=True)


@pytest.fixture(scope="module")
def reg():
    return BlockRegistry()


def test_registry_discovers_many_blocks(reg):
    assert len(reg) > 100  # a full GNU Radio install has hundreds


def test_known_blocks_present(reg):
    assert "blocks_throttle" in reg
    assert "analog_sig_source_x" in reg


def test_describe_block_has_ports_and_params(reg):
    d = reg.describe_block("blocks_throttle")
    assert d["label"]
    assert any(p["id"] == "type" for p in d["parameters"])
    assert len(d["inputs"]) == 1 and len(d["outputs"]) == 1
    assert d["inputs"][0]["domain"] == "stream"


def test_list_blocks_search(reg):
    hits = reg.list_blocks(search="throttle")
    assert any(b["id"] == "blocks_throttle" for b in hits)


def test_resolve_templated_dtype(reg):
    thr = reg.get("blocks_throttle")
    # input dtype is ${type}; resolves to the chosen type parameter value.
    assert resolve_port_dtype(thr.inputs[0], thr, {"type": "complex"}) == "complex"
    assert resolve_port_dtype(thr.inputs[0], thr, {"type": "float"}) == "float"


def test_connection_compatible_same_type(reg):
    thr = reg.get("blocks_throttle")
    c = check_connection(thr, 0, thr, 0, {"type": "complex"}, {"type": "complex"})
    assert c.ok is True


def test_connection_incompatible_type(reg):
    thr = reg.get("blocks_throttle")
    c = check_connection(thr, 0, thr, 0, {"type": "complex"}, {"type": "float"})
    assert c.ok is False
    assert "mismatch" in c.reason


def test_connection_domain_mismatch(reg):
    # Throttle's stream output cannot connect to a message input port.
    thr = reg.get("blocks_throttle")
    sig = reg.get("analog_sig_source_x")
    # analog_sig_source_x input[0] is the 'cmd' message port.
    assert sig.inputs and sig.inputs[0].domain == "message"
    c = check_connection(thr, 0, sig, 0, {"type": "complex"}, {})
    assert c.ok is False
    assert "domain" in c.reason.lower()
