"""The agent-authored chain, executed as a real GNU Radio flowgraph.

The property these tests exist to protect is the one the project previously did not have: the
signal-processing chain the agent writes must determine what it measures. Before
``GRSpecBackend``, ``compile_spec`` kept only the modulation and coding rate, so a chain missing
its matched filter, its pulse shaping and its symbol synchronizer produced bit-identical results
to the full one. If that ever becomes true again, ``test_chain_composition_changes_the_result``
fails.

The second property is subtler and matters just as much: the framework must not quietly do the
agent's work. Grading aligns (an integer symbol offset and one complex gain, fitted against the
known payload) but never recovers timing. A framework that searched sampling phase would make a
chain with no synchronizer appear to work.
"""
import numpy as np
import pytest

from gr_autopilot.flowgraph.builder import compile_spec, validate_spec
from gr_autopilot.flowgraph.spec import FlowgraphSpec
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring import metrics

pytest.importorskip("gnuradio", reason="GNU Radio not installed")
from gr_autopilot.flowgraph.gr_blocks import (  # noqa: E402
    FACTORIES, GraphContext, UnsupportedSkill, make_block, realizable)
from gr_autopilot.link.gr_spec import GRSpecBackend  # noqa: E402

pytestmark = pytest.mark.gnuradio

TX = ("qpsk_mod", "rrc_pulse_shape")
RX = ("rrc_matched_filter", "symbol_sync", "qpsk_demod")


def _run(tx_chain, rx_chain, es_n0_db=12.0, bits=40000, seed=1, **kw):
    be = GRSpecBackend()
    params = LinkParams(modulation=kw.pop("modulation", "qpsk"), n_payload_bits=bits, sps=4,
                        rolloff=0.35, es_n0_db=es_n0_db, tx_chain=tuple(tx_chain),
                        rx_chain=tuple(rx_chain), seed=seed, **kw)
    result = be.run_link(params)
    m = metrics.compute_metrics(result.tx_bits, result.rx_bits, result.rx_syms, result.ref_syms)
    return m, result


# -- the chain is real -------------------------------------------------------

def test_the_canonical_chain_works():
    m, r = _run(TX, RX)
    assert m.ber < 1e-2
    assert r.meta["synchronized"] and r.meta["output_sps"] == pytest.approx(1.0)
    assert r.meta["blocks_instantiated"] == ["qpsk_mod", "rrc_pulse_shape",
                                             "rrc_matched_filter", "symbol_sync"]


def test_chain_composition_changes_the_result():
    """The regression this whole backend exists to prevent: two different chains must not give
    identical numbers. Before this backend, they did."""
    full, _ = _run(TX, RX)
    thin, _ = _run(("qpsk_mod",), ("qpsk_demod",))
    assert full.ber != thin.ber or full.evm_pct != thin.evm_pct


def test_dropping_the_matched_filter_costs_signal_to_noise_ratio():
    """A matched filter maximizes signal-to-noise ratio at the sampling instant, so removing it
    must make the link measurably worse -- not better, and not identical."""
    with_mf, _ = _run(TX, RX)
    without_mf, _ = _run(TX, ("symbol_sync", "qpsk_demod"))
    assert without_mf.ber > with_mf.ber * 3
    assert without_mf.snr_db < with_mf.snr_db - 2.0


def test_omitting_symbol_sync_fails_honestly_rather_than_being_rescued():
    """Without timing recovery the stream never reaches one sample per symbol. The framework must
    report that plainly instead of downsampling on the agent's behalf."""
    m, r = _run(TX, ("rrc_matched_filter", "qpsk_demod"))
    assert not r.meta["synchronized"]
    assert r.meta["output_sps"] > 1.0
    assert "no symbol grid" in r.meta["grading"]
    assert m.ber > 0.3          # a failed link, reported as a failed link


def test_an_unshaped_one_sample_per_symbol_link_still_works():
    """A chain with neither pulse shaping nor a matched filter is unusual but perfectly valid at
    one sample per symbol, and must not be reported as broken. This guards the framework channel:
    an earlier version used a channel block whose internal resampler destroyed such a link even
    with the noise switched off."""
    m, r = _run(("qpsk_mod",), ("qpsk_demod",), es_n0_db=12.0)
    assert m.ber < 1e-3
    assert r.meta["synchronized"]


def test_carrier_recovery_is_what_rescues_a_frequency_offset():
    """With the oscillators offset, the chain WITHOUT carrier recovery must fail and the chain
    WITH it must work. This is the clearest demonstration that block choice has consequences."""
    kw = dict(freq_offset_hz=2000.0, sample_rate=1e6)
    without, _ = _run(TX, RX, **kw)
    with_costas, _ = _run(TX, ("rrc_matched_filter", "symbol_sync", "costas_carrier",
                               "qpsk_demod"), **kw)
    assert without.ber > 0.3
    assert with_costas.ber < 0.1


def test_error_ratio_falls_as_channel_quality_rises():
    bers = [_run(TX, RX, es_n0_db=e)[0].ber for e in (6, 9, 12)]
    assert bers[0] > bers[1] > bers[2]


def test_measured_signal_to_noise_ratio_tracks_the_requested_channel_quality():
    """On the simplest possible chain, where the receiver loses nothing, the measured ratio
    should land close to what was asked for -- confirming the channel calibration."""
    for target in (9.0, 12.0, 15.0):
        m, _ = _run(("qpsk_mod",), ("qpsk_demod",), es_n0_db=target)
        assert abs(m.snr_db - target) < 1.5


# -- block realization -------------------------------------------------------

def test_every_realizable_skill_instantiates():
    ctx = GraphContext(modulation="qpsk", sps=4, rolloff=0.35)
    for name in FACTORIES:
        blk, rate = make_block(name, ctx, cur_sps=4.0)
        assert blk is not None and rate > 0


def test_unknown_skill_is_reported_not_silently_skipped():
    with pytest.raises(UnsupportedSkill, match="no GNU Radio block"):
        make_block("teleporter", GraphContext(), 1.0)


def test_demodulators_are_framework_realized():
    """The symbol decision belongs to the scoring layer, which owns the known payload, so a
    demodulator is named for validation but placed as no block."""
    blk, rate = make_block("qpsk_demod", GraphContext(), 1.0)
    assert blk is None and rate == 1.0 and realizable("qpsk_demod")


def test_costas_refuses_a_constellation_it_cannot_track():
    """A Costas loop needs constant modulus; 16-QAM does not have it. Refusing loudly is better
    than a silently ringing constellation."""
    with pytest.raises(UnsupportedSkill, match="constant-modulus"):
        make_block("costas_carrier", GraphContext(modulation="16qam"), 2.0)


def test_symbol_sync_configures_itself_from_the_rate_that_reaches_it():
    ctx = GraphContext(sps=8)
    _, rate_at_4 = make_block("symbol_sync", ctx, cur_sps=4.0)
    _, rate_at_2 = make_block("symbol_sync", ctx, cur_sps=2.0)
    assert rate_at_4 == pytest.approx(0.25) and rate_at_2 == pytest.approx(0.5)


# -- the seam ----------------------------------------------------------------

def test_compile_spec_carries_the_chains_through():
    """The specific defect this replaced: the chains used to be dropped here."""
    spec = FlowgraphSpec(structure_id="s", modulation="qpsk", tx_chain=list(TX), rx_chain=list(RX))
    out = compile_spec(spec)
    assert out["tx_chain"] == TX and out["rx_chain"] == RX


def test_new_skills_are_offered_to_the_agent():
    """A block the agent cannot name is a block it cannot use, so the registry and the block
    factories must not drift apart."""
    spec = FlowgraphSpec(structure_id="s", modulation="qpsk", tx_chain=list(TX),
                         rx_chain=["rrc_matched_filter", "symbol_sync", "costas_carrier",
                                   "agc", "qpsk_demod"])
    assert validate_spec(spec).ok


def test_backend_refuses_to_invent_a_chain():
    """Given no chain there is nothing to execute; guessing one would be the old behaviour."""
    with pytest.raises(ValueError, match="tx_chain and rx_chain"):
        GRSpecBackend().run_link(LinkParams(modulation="qpsk", es_n0_db=12.0))


def test_hidden_channel_quality_is_not_echoed_back_in_metadata():
    _, r = _run(TX, RX, es_n0_db=12.0)
    assert r.meta["es_n0_db"] is None
    assert "12" not in str(r.meta.get("grading", ""))
