"""Realizing skills as actual in-tree GNU Radio blocks.

Until now a "skill" was a metadata row: a name, its input and output data types, and a
documentation string, with nothing behind it. The agent's ``tx_chain`` and ``rx_chain`` were
validated for plausibility and then discarded -- ``compile_spec`` kept only the modulation and
the coding rate, so two different chains produced bit-identical results. This module is what
gives a chain teeth: every entry maps to a factory that instantiates a real block from the
GNU Radio in-tree block library, which is then wired into a real flowgraph by
:mod:`gr_autopilot.link.gr_spec`.

Consequences worth stating plainly, because they are the point:

* Omitting ``rrc_matched_filter`` really does leave the noise unfiltered, so the bit error
  ratio really does get worse.
* Omitting ``symbol_sync`` really does leave the stream at the sampling rate rather than one
  sample per symbol, so the link really does fail.

The framework never silently supplies a missing stage. Grading performs alignment only -- an
integer symbol offset plus one complex gain, fitted against the known transmitted payload -- and
never timing recovery, because doing the agent's synchronization for it would hide exactly the
failure the agent is supposed to discover.

Not yet agent-placeable, and deliberately so: automatic gain control and the symbol decision
stage. Bit error ratio grading is framework-owned throughout this project (the framework holds
the known payload), so the decision is taken by the scoring layer against the aligned symbols,
as it is for every other backend including the radios.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from gr_autopilot import modulation


@dataclass
class GraphContext:
    """Everything a block factory needs that is not part of the skill's own name."""

    modulation: str = "qpsk"
    sps: int = 4                        # samples per symbol
    rolloff: float = 0.35               # root-raised-cosine excess bandwidth
    ntaps_per_symbol: int = 8
    timing_loop_bw: float = 2 * np.pi * 0.02
    carrier_loop_bw: float = 2 * np.pi * 0.02

    @property
    def constellation(self):
        return modulation.get(self.modulation)

    def rrc_taps(self):
        from gnuradio.filter import firdes
        ntaps = self.ntaps_per_symbol * self.sps + 1        # odd, so the delay is a whole sample
        return firdes.root_raised_cosine(1.0, float(self.sps), 1.0, self.rolloff, ntaps)


# -- factories ---------------------------------------------------------------
# Each returns (block, rate_out_per_in). The rate is bookkeeping so the backend can report the
# samples-per-symbol the chain actually produces, and therefore tell a correctly-decimated
# stream from one the agent forgot to synchronize.

def _modulator(ctx: GraphContext, cur_sps: float):
    """Bits to complex symbols. ``chunks_to_symbols_bc`` maps each input byte, treated as a
    symbol index, to a constellation point -- and the project's own constellation table is
    indexed by exactly that integer, so the mapping is identical to the reference backend's."""
    from gnuradio import digital
    pts = [complex(p) for p in ctx.constellation.points]
    return digital.chunks_to_symbols_bc(pts, 1), 1.0


def _pulse_shape(ctx: GraphContext, cur_sps: float):
    """Interpolating root-raised-cosine transmit filter: one symbol in, ``sps`` samples out."""
    from gnuradio import filter as grfilter
    return grfilter.interp_fir_filter_ccf(ctx.sps, ctx.rrc_taps()), float(ctx.sps)


def _matched_filter(ctx: GraphContext, cur_sps: float):
    """Receive filter matched to the transmit pulse.

    It also decimates to two samples per symbol, because that is what the timing-error detector
    downstream expects: Gardner's detector compares a symbol-centre sample with the mid-point
    between symbols, so it needs two samples per symbol and becomes unstable when handed more.
    (This project measured the same constraint on the radios.) When fewer than two samples per
    symbol arrive there is nothing to decimate and the filter passes the rate through.
    """
    from gnuradio import filter as grfilter
    decim = max(1, int(cur_sps // 2))
    return grfilter.fir_filter_ccf(decim, ctx.rrc_taps()), 1.0 / decim


def _symbol_sync(ctx: GraphContext, cur_sps: float):
    """Symbol timing recovery (Gardner timing-error detector), decimating to one sample per
    symbol. Without this stage nothing downstream sees a symbol grid at all.

    Configured from the rate that actually reaches it rather than from the transmit rate, so the
    block is correct wherever the agent places it in the chain.
    """
    from gnuradio import digital
    sps_in = max(2.0, float(cur_sps))    # the block requires at least 2 samples per symbol
    blk = digital.symbol_sync_cc(
        digital.TED_GARDNER, sps_in, ctx.timing_loop_bw,
        1.0,    # damping
        1.0,    # timing-error-detector gain
        1.5,    # maximum deviation
        1,      # output samples per symbol
        digital.constellation_qpsk().base(),
    )
    return blk, 1.0 / sps_in


def _costas(ctx: GraphContext, cur_sps: float):
    """Carrier phase and frequency recovery. Rings a non-constant-modulus constellation such as
    16-QAM, which is a real property of the loop and not a defect in this wiring."""
    from gnuradio import digital
    order = {"bpsk": 2, "qpsk": 4, "8psk": 8}.get(ctx.modulation)
    if order is None:
        raise UnsupportedSkill(
            f"costas_carrier does not support {ctx.modulation!r}: a Costas loop needs a "
            "constant-modulus constellation (BPSK, QPSK or 8-PSK), not QAM")
    return digital.costas_loop_cc(ctx.carrier_loop_bw, order, False), 1.0


def _agc(ctx: GraphContext, cur_sps: float):
    """Automatic gain control: normalizes amplitude ahead of amplitude-sensitive stages."""
    from gnuradio import analog
    return analog.agc2_cc(1e-3, 1e-3, 1.0, 1.0), 1.0


class UnsupportedSkill(ValueError):
    """A skill has no in-tree block realization, or none valid in this context."""


#: skill name -> factory. Demodulators are absent on purpose: the decision stage belongs to the
#: framework's grading (see the module docstring), so a chain names its demodulator for
#: structural validation but the block itself is never placed.
FACTORIES = {
    "bpsk_mod": _modulator,
    "qpsk_mod": _modulator,
    "psk8_mod": _modulator,
    "qam16_mod": _modulator,
    "qam32_mod": _modulator,
    "qam64_mod": _modulator,
    "qam256_mod": _modulator,
    "rrc_pulse_shape": _pulse_shape,
    "rrc_matched_filter": _matched_filter,
    "symbol_sync": _symbol_sync,
    "costas_carrier": _costas,
    "agc": _agc,
}

#: Skills that are structural only -- validated in the chain, realized by the scoring layer.
FRAMEWORK_REALIZED = {"bpsk_demod", "qpsk_demod", "psk8_demod", "qam16_demod",
                      "qam32_demod", "qam64_demod", "qam256_demod"}


def make_block(name: str, ctx: GraphContext, cur_sps: float = 1.0):
    """Instantiate one skill as a real GNU Radio block.

    ``cur_sps`` is how many samples per symbol reach this position in the chain, so a block can
    configure itself for where the agent actually put it rather than assuming a fixed pipeline.

    Returns ``(block, rate_out_per_in)``, or ``(None, 1.0)`` for a skill the framework realizes.
    """
    if name in FRAMEWORK_REALIZED:
        return None, 1.0
    try:
        factory = FACTORIES[name]
    except KeyError:
        raise UnsupportedSkill(
            f"no GNU Radio block realizes skill {name!r}; known: "
            f"{sorted(set(FACTORIES) | FRAMEWORK_REALIZED)}") from None
    return factory(ctx, cur_sps)


def realizable(name: str) -> bool:
    return name in FACTORIES or name in FRAMEWORK_REALIZED
