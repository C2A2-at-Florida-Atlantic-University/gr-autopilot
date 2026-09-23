"""GNU Radio simulation link backend.

Runs a real GNU Radio flowgraph:

    tx symbols -> RRC pulse-shape (interp x sps) -> channels.channel_model
                                                 -> RRC matched filter (decim / sps) -> rx

This exercises the same DSP path (pulse shaping, a channel with AWGN + optional impairments,
matched filtering) that the PlutoSDR hardware backend will later drive, so the loop above
the LinkBackend seam is identical for sim and hardware.

Driven by ``noise_voltage`` (the channel_model noise std) rather than an exact Es/N0: the
absolute mapping from noise_voltage to SNR depends on filter gains, so we *measure* the
resulting SNR (data-aided, in the scoring layer) instead of asserting it. The pure-numpy
backend owns the exact-theory check.

GNU Radio is imported lazily inside ``run_link`` so importing this module needs no GNU Radio.

Current limitation: carrier-frequency-offset handling needs a synchronization block that is
not wired yet, so ``freq_offset_hz`` must be 0 here (a later milestone adds carrier recovery).
"""
from __future__ import annotations

import numpy as np

from gr_autopilot import modulation
from gr_autopilot.link.backend import LinkBackend, LinkParams, LinkResult
from gr_autopilot.scoring.payload import known_payload


def _best_alignment(
    rx_out: np.ndarray, tx_syms: np.ndarray, max_offset: int = 64
) -> tuple[np.ndarray, int, complex, int]:
    """Align the recovered symbol stream to the known transmitted symbols.

    Filtering drops a few symbols of transient at the head (group delay) and tail, so the
    recovered stream is shifted and slightly shorter than the payload. Searches integer
    symbol offsets and, for each, fits a least-squares complex gain (removing amplitude and
    constant phase) over the overlap, choosing the offset with the smallest per-symbol
    residual. Returns ``(rx_aligned_normalized, offset, gain, overlap_len)``; the caller
    trims the transmitted side to ``overlap_len`` so tx and rx correspond one-to-one.
    Data-aided (uses the known payload) — legitimate because this is framework-owned grading.
    """
    n = tx_syms.size
    if rx_out.size == 0:
        raise ValueError("empty receiver output")

    best = None
    hi = min(max_offset, rx_out.size - 1)
    for d in range(0, hi + 1):
        length = min(n, rx_out.size - d)
        if length < n // 2:  # too little overlap to trust
            break
        seg = rx_out[d : d + length]
        tx = tx_syms[:length]
        g = np.vdot(tx, seg) / np.vdot(tx, tx)
        if g == 0:
            continue
        resid = seg - g * tx
        err = float(np.sum(np.abs(resid) ** 2)) / length  # per-symbol, comparable across offsets
        if best is None or err < best[0]:
            best = (err, d, complex(g), length)
    if best is None:
        raise ValueError("symbol alignment failed")
    _, d, g, length = best
    rx_aligned = rx_out[d : d + length] / g
    return rx_aligned, d, g, length


def _recover_symbols(
    hi_rate: np.ndarray, tx_syms: np.ndarray, sps: int, max_offset: int = 64
) -> tuple[np.ndarray, int, complex, int, int]:
    """Data-aided symbol timing recovery.

    The matched filter runs at full sample rate, so we must pick which of the ``sps``
    sampling phases lands on the symbol peaks. Chooses the phase (and integer offset) whose
    downsampled stream best matches the known symbols after gain normalization. Amplitude-
    sensitive constellations (16-QAM) need the exact peak; a half-sample error that QPSK's
    sign decisions shrug off shows up here as inter-symbol interference.

    Returns ``(rx_aligned_normalized, offset, gain, overlap_len, sample_phase)``.
    """
    best = None
    for phase in range(sps):
        stream = hi_rate[phase::sps]
        if stream.size < tx_syms.size // 2:
            continue
        rx_al, d, g, length = _best_alignment(stream, tx_syms, max_offset=max_offset)
        resid = float(np.mean(np.abs(rx_al - tx_syms[:length]) ** 2))
        if best is None or resid < best[0]:
            best = (resid, rx_al, d, g, length, phase)
    if best is None:
        raise ValueError("symbol timing recovery failed")
    _, rx_al, d, g, length, phase = best
    return rx_al, d, g, length, phase


class GrSimBackend(LinkBackend):
    name = "gr-sim"

    def __init__(self, ntaps_per_symbol: int = 8):
        # RRC filter length in symbols (=> ntaps = ntaps_per_symbol*sps + 1, odd).
        self.ntaps_per_symbol = ntaps_per_symbol

    def run_link(self, params: LinkParams) -> LinkResult:
        from gnuradio import blocks, channels, gr
        from gnuradio import filter as grfilter
        from gnuradio.filter import firdes

        if params.freq_offset_hz != 0.0:
            raise NotImplementedError(
                "GrSimBackend has no carrier recovery yet; set freq_offset_hz=0 "
                "(CFO handling is a later milestone)"
            )
        noise_voltage = 0.0 if params.noise_voltage is None else float(params.noise_voltage)

        const = modulation.get(params.modulation)
        bps = const.bits_per_symbol
        sps = int(params.sps)

        n_syms = params.n_payload_bits // bps
        n_bits = n_syms * bps
        tx_bits = known_payload(n_bits, order=params.prbs_order, seed=params.seed)
        tx_syms = const.modulate(tx_bits)

        ntaps = self.ntaps_per_symbol * sps + 1
        rrc = firdes.root_raised_cosine(1.0, float(sps), 1.0, params.rolloff, ntaps)

        tb = gr.top_block()
        src = blocks.vector_source_c(tx_syms.tolist(), False, 1, [])
        tx_rrc = grfilter.interp_fir_filter_ccf(sps, rrc)
        chan = channels.channel_model(
            noise_voltage=noise_voltage,
            frequency_offset=0.0,
            epsilon=1.0,
            taps=[1.0 + 0.0j],
            noise_seed=int(params.seed),
            block_tags=False,
        )
        rx_rrc = grfilter.fir_filter_ccf(1, rrc)  # matched filter at full rate; timing in numpy
        sink = blocks.vector_sink_c()
        tb.connect(src, tx_rrc, chan, rx_rrc, sink)
        tb.run()

        hi_rate = np.asarray(sink.data(), dtype=np.complex128)
        rx_syms, offset, gain, length, sample_phase = _recover_symbols(hi_rate, tx_syms, sps)

        # Trim the transmitted side to the aligned overlap so tx and rx correspond 1:1.
        tx_syms_al = tx_syms[:length]
        tx_bits_al = tx_bits[: length * bps]
        rx_bits, _ = const.hard_decision(rx_syms)      # decisions -> bits

        return LinkResult(
            modulation=const.name,
            tx_bits=tx_bits_al,
            rx_bits=rx_bits,
            tx_syms=tx_syms_al,
            rx_syms=rx_syms,
            ref_syms=tx_syms_al,   # data-aided: EVM/SNR reference is the KNOWN tx (rx is gain-normalized)
            meta={
                "backend": self.name,
                "noise_voltage": noise_voltage,
                "sps": sps,
                "rolloff": params.rolloff,
                "n_syms": n_syms,
                "n_syms_used": length,
                "align_offset": offset,
                "sample_phase": sample_phase,
                "gain_abs": float(abs(gain)),
            },
        )
