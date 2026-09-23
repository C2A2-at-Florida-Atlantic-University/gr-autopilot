"""Flowgraph spec / skills / builder tests, including the M2 exit criterion."""
import pytest

from gr_autopilot.flowgraph import (
    FlowgraphSpec,
    SkillsRegistry,
    build_and_run,
    compile_spec,
    validate_spec,
)
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.scoring.metrics import compute_metrics


# ---- spec + skills ----------------------------------------------------------

def test_spec_json_roundtrip():
    spec = FlowgraphSpec.link("qpsk", sps=4, rolloff=0.3)
    assert FlowgraphSpec.from_json(spec.to_json()).to_dict() == spec.to_dict()


def test_skills_registry_basics():
    reg = SkillsRegistry()
    names = {s["name"] for s in reg.list_skills()}
    assert {"qpsk_mod", "rrc_pulse_shape", "qpsk_demod", "awgn_channel"} <= names
    assert reg.describe_skill("qpsk_mod")["modulation"] == "qpsk"
    assert reg.modulator_for("16qam").name == "qam16_mod"


@pytest.mark.parametrize("mod", ["bpsk", "qpsk", "16qam"])
def test_canonical_links_validate(mod):
    assert validate_spec(FlowgraphSpec.link(mod)).ok


# ---- validation catches bad specs ------------------------------------------

def test_validate_rejects_demod_mismatch():
    spec = FlowgraphSpec.link("qpsk")
    spec.rx_chain[-1] = "bpsk_demod"  # wrong demod for a QPSK link
    res = validate_spec(spec)
    assert not res.ok and any("demodulator" in e for e in res.errors)


def test_validate_rejects_dtype_break():
    spec = FlowgraphSpec.link("qpsk")
    spec.rx_chain = ["qpsk_mod", "qpsk_demod"]  # rx must start from complex, not bits
    res = validate_spec(spec)
    assert not res.ok and any("consumes" in e or "mismatch" in e for e in res.errors)


def test_validate_rejects_unknown_skill():
    spec = FlowgraphSpec.link("qpsk")
    spec.tx_chain = ["qpsk_mod", "magic_filter"]
    res = validate_spec(spec)
    assert not res.ok and any("unknown skill" in e for e in res.errors)


def test_validate_rejects_framework_owned_placement():
    spec = FlowgraphSpec.link("qpsk")
    spec.tx_chain = ["qpsk_mod", "awgn_channel", "rrc_pulse_shape"]
    res = validate_spec(spec)
    assert not res.ok and any("framework-owned" in e for e in res.errors)


def test_validate_rejects_bad_sps():
    spec = FlowgraphSpec.link("qpsk", sps=4)
    spec.pulse_shape["sps"] = 99
    res = validate_spec(spec)
    assert not res.ok and any("sps" in e for e in res.errors)


def test_build_and_run_raises_on_invalid():
    spec = FlowgraphSpec.link("qpsk")
    spec.tx_chain = ["magic_filter"]
    with pytest.raises(ValueError, match="invalid flowgraph spec"):
        build_and_run(spec, NumpySimBackend(), n_payload_bits=1000, es_n0_db=10.0)


# ---- M2 EXIT: recompose the link from a spec => identical BER ---------------

def test_compile_spec_extracts_params():
    c = compile_spec(FlowgraphSpec.link("16qam", sps=8, rolloff=0.22))
    assert (c["modulation"], c["sps"], c["rolloff"], c["coding"]) == ("16qam", 8, 0.22, None)
    coded = compile_spec(FlowgraphSpec.link("16qam", coding="conv_k3_r12"))
    assert coded["coding"] == "conv_k3_r12"


def test_compile_spec_carries_the_chains_rather_than_dropping_them():
    """This previously reduced the whole specification to four scalars, discarding the chains --
    which is why two different chains used to produce identical results. Backends with a fixed
    internal pipeline still ignore them; GRSpecBackend executes them (see test_gr_spec.py)."""
    c = compile_spec(FlowgraphSpec.link("qpsk"))
    assert c["tx_chain"] and c["rx_chain"]
    assert "qpsk_mod" in c["tx_chain"]


@pytest.mark.parametrize("mod", ["bpsk", "qpsk", "16qam"])
def test_spec_recomposes_link_identically_numpy(mod):
    # The M2 exit criterion: a flowgraph built by composing skills through the spec must
    # produce the SAME link (identical BER) as the hand-wired LinkParams path.
    be = NumpySimBackend()
    spec = FlowgraphSpec.link(mod, sps=4, rolloff=0.35)
    r_spec = build_and_run(spec, be, n_payload_bits=200_000, es_n0_db=12.0, seed=1)
    r_direct = be.run_link(
        LinkParams(modulation=mod, n_payload_bits=200_000, sps=4, rolloff=0.35, es_n0_db=12.0, seed=1)
    )
    m_spec = compute_metrics(r_spec.tx_bits, r_spec.rx_bits, r_spec.rx_syms, r_spec.ref_syms)
    m_direct = compute_metrics(r_direct.tx_bits, r_direct.rx_bits, r_direct.rx_syms, r_direct.ref_syms)
    assert m_spec.n_errors == m_direct.n_errors
    assert m_spec.ber == m_direct.ber
