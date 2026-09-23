"""Pure-numpy AWGN link backend.

The fast, dependency-light backend: symbol-rate additive white Gaussian noise at an exact
Es/N0, with no pulse shaping or synchronization. Its purpose is to (a) validate the
scoring layer against closed-form BER theory and (b) give a hardware-free reference the
whole loop can run against. It ignores ``sps``, ``rolloff``, ``noise_voltage`` and
``freq_offset_hz`` (those belong to the pulse-shaped backends).
"""
from __future__ import annotations

import numpy as np

from gr_autopilot import modulation
from gr_autopilot.coding import (
    CODING_RATES, coding_decode, coding_decode_soft, coding_encode, info_bits_for_budget)
from gr_autopilot.link.backend import LinkBackend, LinkParams, LinkResult
from gr_autopilot.scoring.payload import known_payload


class NumpySimBackend(LinkBackend):
    name = "numpy-sim"

    def run_link(self, params: LinkParams) -> LinkResult:
        if params.es_n0_db is None:
            raise ValueError("NumpySimBackend requires params.es_n0_db")
        if params.coding is not None and params.coding not in CODING_RATES:
            raise ValueError(f"NumpySimBackend: unknown coding {params.coding!r}")

        const = modulation.get(params.modulation)
        bps = const.bits_per_symbol
        n_syms = params.n_payload_bits // bps  # symbol budget for the channel (fixed rate)

        # Build the transmitted symbols. Uncoded: info bits map 1:1 to symbols. Coded: a smaller
        # info payload is convolutionally encoded to fill the SAME symbol budget, so at a fixed
        # channel Es/N0 the coded link trades throughput for coding gain (the AMC-with-coding
        # tradeoff). BER is graded on the (post-decode) info bits either way.
        if params.coding is not None:
            n_info = info_bits_for_budget(n_syms * bps, params.coding)
            info_bits = known_payload(n_info, order=params.prbs_order, seed=params.seed)
            coded_bits = coding_encode(info_bits, params.coding)
            coded_len = coded_bits.size
            pad = (-coded_len) % bps
            mod_bits = np.concatenate([coded_bits, np.zeros(pad, dtype=coded_bits.dtype)])
            tx_syms = const.modulate(mod_bits)
            tx_bits = info_bits
        else:
            n_bits = n_syms * bps
            tx_bits = known_payload(n_bits, order=params.prbs_order, seed=params.seed)
            tx_syms = const.modulate(tx_bits)

        # AWGN at the requested Es/N0. Es = 1 by construction, so N0 = 1 / (Es/N0).
        es_n0_lin = 10.0 ** (params.es_n0_db / 10.0)
        sigma = np.sqrt((1.0 / es_n0_lin) / 2.0)  # per real/imag axis
        rng = np.random.default_rng(params.seed)
        noise = sigma * (rng.standard_normal(tx_syms.size) + 1j * rng.standard_normal(tx_syms.size))

        # Optional residual constant carrier phase (channel) and the receiver's de-rotation knob.
        if params.phase_offset_rad:
            rx_syms = tx_syms * np.exp(1j * params.phase_offset_rad) + noise
        else:
            rx_syms = tx_syms + noise
        if params.phase_correction_rad:
            rx_syms = rx_syms * np.exp(-1j * params.phase_correction_rad)

        demod_bits, _ = const.hard_decision(rx_syms)   # decisions -> bits
        if params.coding is not None:
            if params.soft_decision:                                   # LLRs -> soft Viterbi (~2 dB)
                rx_bits = coding_decode_soft(const.soft_bits(rx_syms)[:coded_len], params.coding, n_info)
            else:
                rx_bits = coding_decode(demod_bits[:coded_len], params.coding, n_info)
        else:
            rx_bits = demod_bits

        return LinkResult(
            modulation=const.name,
            tx_bits=tx_bits,
            rx_bits=rx_bits,
            tx_syms=tx_syms,
            rx_syms=rx_syms,
            ref_syms=tx_syms,        # data-aided: EVM/SNR reference is the KNOWN tx, not the decisions
            meta={"backend": self.name, "es_n0_db": params.es_n0_db,
                  "n_syms": int(tx_syms.size), "coding": params.coding},
        )
