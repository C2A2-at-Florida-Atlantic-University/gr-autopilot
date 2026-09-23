#!/usr/bin/env python3
"""Soft-decision Viterbi coding gain — ~2 dB from demodulator LLRs (sim, no radios).

Prints coded-QPSK info-BER vs Es/N0 for hard- vs soft-decision decoding: the soft decoder exploits how
*confident* each demodulated bit was (LLR magnitude), not just the hard bit, buying ~2 dB on every
coded rung. The figure is scripts/paper_figures.py -> runs/figures/fig6_soft_decision.png.

Run: python scripts/soft_decision_gain.py
"""
from __future__ import annotations

import sys

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.scoring.metrics import compute_metrics


def _ber(be, es, soft, bits=120_000):
    r = be.run_link(LinkParams(modulation="qpsk", coding="conv_k3_r12", n_payload_bits=bits,
                               es_n0_db=es, soft_decision=soft, seed=3))
    return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms).ber


def main() -> int:
    be = NumpySimBackend()
    print("coded QPSK (rate-1/2 K=3) — hard vs soft-decision Viterbi:")
    print(f"  {'Es/N0':>6} {'hard':>11} {'soft':>11}")
    for es in (2, 3, 4, 5, 6):
        print(f"  {es:6.0f} {_ber(be, es, False):11.2e} {_ber(be, es, True):11.2e}", flush=True)
    print("\n=> soft-decision buys ~2 dB (the BER curve shifts left) — for free, on the same channel.")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
