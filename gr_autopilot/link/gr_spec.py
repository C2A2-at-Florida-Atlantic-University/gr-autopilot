"""A link backend that executes the chain the agent actually wrote.

Every other simulation backend in this project takes a modulation and a channel quality and runs
a fixed, hand-written signal-processing pipeline. This one takes the ``tx_chain`` and ``rx_chain``
from the agent's flowgraph specification and builds a real GNU Radio flowgraph out of in-tree
blocks, in the order the agent listed them:

    payload bits
      -> [ transmit chain ]        e.g. qpsk_mod, rrc_pulse_shape
      -> channel model             framework-owned: additive noise, carrier offset
      -> [ receive chain ]         e.g. rrc_matched_filter, symbol_sync
      -> grading

Because the chain is executed rather than described, the agent's structural choices have
consequences it can measure. Dropping the matched filter costs signal-to-noise ratio; dropping
the symbol synchronizer leaves the stream at the sampling rate instead of one sample per symbol
and the link fails outright.

Grading is deliberately weak, and that is the important part. It performs alignment only: it
searches a small range of integer symbol offsets and fits one complex gain, both against the
known transmitted payload. It does NOT search sampling phase. A framework that searched sampling
phase would be performing timing recovery on the agent's behalf, and a chain missing its
synchronizer would then appear to work -- concealing precisely the failure the agent needs to
observe. So the symbol decision is framework-owned (as everywhere in this project, since the
framework holds the known payload), but synchronization is not.

Channel quality is expressed as ``es_n0_db``, the ratio of energy per symbol to noise spectral
density, and converted to the noise voltage the channel-model block expects. The pure-numpy
backend remains the reference against which bit-error-ratio theory is checked; this backend is
about structure, and its absolute noise calibration is approximate by comparison.
"""
from __future__ import annotations

import numpy as np

from gr_autopilot import modulation
from gr_autopilot.flowgraph.gr_blocks import GraphContext, UnsupportedSkill, make_block
from gr_autopilot.link.backend import LinkBackend, LinkParams, LinkResult
from gr_autopilot.link.gr_sim import _best_alignment
from gr_autopilot.scoring.payload import known_payload


class GRSpecBackend(LinkBackend):
    """Executes the agent-authored chain as a GNU Radio flowgraph."""

    name = "gr-spec"

    def __init__(self, ntaps_per_symbol: int = 8, max_align_offset: int = 96,
                 timing_loop_bw: float = 2 * np.pi * 0.01):
        self.ntaps_per_symbol = int(ntaps_per_symbol)
        self.max_align_offset = int(max_align_offset)
        # Normalized loop bandwidth of the symbol timing recovery loop. Measured on this backend
        # across signal levels 6-14 dB and 4 noise seeds:
        #
        #   2*pi*0.020   lost lock on one seed at 10 dB -- a link that fails completely at one
        #                signal level while working either side of it, which is far more
        #                confusing to an agent than a link that is merely poor
        #   2*pi*0.010   no lock failures, and the lowest error floor          <- chosen
        #   2*pi*0.005   no failures, but the error floor rises to ~6e-3
        #   2*pi*0.002   error floor ~2e-2
        #
        # Narrower is not automatically better: a slow loop spends longer acquiring, and over a
        # finite payload that unlocked prefix dominates the error count. The same order of
        # magnitude was measured independently on the radios for the hardware backend.
        self.timing_loop_bw = float(timing_loop_bw)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _symbol_indices(bits: np.ndarray, bps: int) -> np.ndarray:
        """Pack a bit vector into one integer per symbol (first bit is most significant), which
        is the byte stream ``chunks_to_symbols_bc`` expects."""
        b = bits.reshape(-1, bps)
        weights = (1 << np.arange(bps - 1, -1, -1)).astype(np.int64)
        return (b * weights).sum(axis=1).astype(np.uint8)

    @staticmethod
    def _noise_voltage(es_n0_db: float, signal_power: float, sps_at_channel: float) -> float:
        """Noise amplitude for the channel model, from the MEASURED transmitted signal.

        The channel quality is defined where a channel exists -- at its own input -- rather than
        at the decision point. Energy per symbol is the transmitted power times the number of
        samples that symbol occupies; the noise spectral density is the per-sample noise power.
        Deriving this from a measurement of the actual transmit chain, instead of assuming a
        particular filter gain, keeps the definition honest for any chain the agent writes.

        This matters for the experiment's meaning: the channel offers a fixed energy-per-symbol
        to noise-density ratio, and how much of it the receiver realizes is exactly what the
        agent's chain determines. A weak chain therefore measures worse, as it should.
        """
        es_n0 = 10.0 ** (float(es_n0_db) / 10.0)
        es = float(signal_power) * float(max(sps_at_channel, 1.0))
        return float(np.sqrt(max(es / es_n0, 0.0)))

    def _build_chain(self, names, ctx, start_sps: float = 1.0):
        """Instantiate a chain, tracking samples-per-symbol so each block is configured for the
        position the agent actually gave it."""
        blocks_out, rate = [], float(start_sps)
        for name in names:
            blk, r = make_block(name, ctx, rate)
            if blk is not None:
                blocks_out.append((name, blk))
                rate *= r
        return blocks_out, rate

    def _measure_tx_power(self, indices, tx_names, ctx) -> tuple[float, float]:
        """Run the transmit chain alone, without noise, to measure what it actually emits.

        Returns ``(mean_power, samples_per_symbol)`` at the channel input.
        """
        from gnuradio import blocks, gr
        tb = gr.top_block()
        src = blocks.vector_source_b(indices.tolist(), False, 1, [])
        chain, rate = self._build_chain(tx_names, ctx)
        sink = blocks.vector_sink_c()
        tb.connect(*([src] + [b for _, b in chain] + [sink]))
        tb.run()
        out = np.asarray(sink.data(), dtype=np.complex128)
        power = float(np.mean(np.abs(out) ** 2)) if out.size else 0.0
        return power, rate

    # -- one trial -----------------------------------------------------------
    def run_link(self, params: LinkParams) -> LinkResult:
        from gnuradio import analog, blocks, gr

        if params.es_n0_db is None:
            raise ValueError("GRSpecBackend requires params.es_n0_db")
        tx_names = list(params.tx_chain or ())
        rx_names = list(params.rx_chain or ())
        if not tx_names or not rx_names:
            raise ValueError(
                "GRSpecBackend needs tx_chain and rx_chain: it executes the agent's chain, so "
                "there is nothing to run without one")

        const = modulation.get(params.modulation)
        bps = const.bits_per_symbol
        sps = int(params.sps)
        # A loop bandwidth carried on the parameters wins over the backend's default, so a value
        # edited into an exported flowgraph reaches the block that uses it.
        ctx = GraphContext(modulation=params.modulation, sps=sps, rolloff=params.rolloff,
                           ntaps_per_symbol=self.ntaps_per_symbol,
                           timing_loop_bw=(params.timing_loop_bw
                                           if params.timing_loop_bw is not None
                                           else self.timing_loop_bw))

        n_syms = params.n_payload_bits // bps
        tx_bits = known_payload(n_syms * bps, order=params.prbs_order, seed=params.seed)
        tx_syms = const.modulate(tx_bits)
        indices = self._symbol_indices(tx_bits, bps)

        # Measure the transmit chain before adding noise, so the channel quality is defined
        # against what this particular chain actually emits.
        tx_power, tx_rate_measured = self._measure_tx_power(indices, tx_names, ctx)

        tx_blocks, tx_rate = self._build_chain(tx_names, ctx)
        rx_blocks, rx_rate = self._build_chain(rx_names, ctx, start_sps=tx_rate)

        # The framework-owned channel: additive white Gaussian noise, plus a carrier frequency
        # offset when one is asked for. Built from primitives rather than from the library's
        # channel_model block, because that block also runs a timing-offset resampler whose
        # fractional delay silently destroys a stream carrying one sample per symbol (measured:
        # it corrupts such a link even with the noise set to zero, and drops samples). A channel
        # that quietly resamples would attribute a chain's success or failure to something the
        # agent cannot see or control.
        amplitude = self._noise_voltage(params.es_n0_db, tx_power, tx_rate_measured)

        tb = gr.top_block()
        src = blocks.vector_source_b(indices.tolist(), False, 1, [])
        noise = analog.noise_source_c(analog.GR_GAUSSIAN, amplitude, int(params.seed))
        adder = blocks.add_cc()
        sink = blocks.vector_sink_c()

        pre_channel = [src] + [b for _, b in tx_blocks]
        if params.freq_offset_hz:
            cycles_per_sample = 2 * np.pi * float(params.freq_offset_hz) / float(params.sample_rate)
            pre_channel.append(blocks.rotator_cc(cycles_per_sample))
        post_channel = [b for _, b in rx_blocks] + [sink]

        try:
            tb.connect(*pre_channel)
            tb.connect(pre_channel[-1], (adder, 0))
            tb.connect(noise, (adder, 1))
            tb.connect(*([adder] + post_channel))
        except Exception as exc:                     # a type mismatch between adjacent blocks
            raise UnsupportedSkill(
                f"chain does not connect: {' -> '.join(tx_names + ['channel'] + rx_names)} ({exc})"
            ) from exc
        tb.run()

        out = np.asarray(sink.data(), dtype=np.complex128)
        # Samples per symbol the chain actually produced. 1.0 means the receive chain
        # synchronized; anything else means it did not, and grading will reflect that.
        out_sps = rx_rate

        meta = {
            "backend": self.name,
            "tx_chain": tx_names, "rx_chain": rx_names,
            "blocks_instantiated": [n for n, _ in tx_blocks] + [n for n, _ in rx_blocks],
            "sps": sps, "rolloff": params.rolloff,
            "es_n0_db": None,                        # never echo the hidden channel back
            "output_sps": out_sps,
            "synchronized": abs(out_sps - 1.0) < 1e-9,
        }

        if out.size < 8 or not meta["synchronized"]:
            # An unsynchronized stream has no symbol grid to grade against. Report an honest
            # failure rather than quietly downsampling for the agent.
            length = min(n_syms, max(out.size, 1))
            rx_syms = np.zeros(length, dtype=complex)
            meta["grading"] = "no symbol grid: receive chain did not decimate to 1 sample/symbol"
        else:
            try:
                rx_syms, _d, _g, length = _best_alignment(
                    out, tx_syms, max_offset=self.max_align_offset)
                meta["grading"] = "aligned"
            except ValueError as exc:
                length = min(n_syms, out.size)
                rx_syms = np.zeros(length, dtype=complex)
                meta["grading"] = f"alignment failed: {exc}"

        tx_syms_al = tx_syms[:length]
        tx_bits_al = tx_bits[: length * bps]
        rx_bits, _ = const.hard_decision(rx_syms)
        return LinkResult(
            modulation=const.name,
            tx_bits=tx_bits_al, rx_bits=rx_bits[: tx_bits_al.size],
            tx_syms=tx_syms_al, rx_syms=rx_syms,
            ref_syms=tx_syms_al, meta=meta)
