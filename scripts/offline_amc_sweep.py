#!/usr/bin/env python3
"""Offline AMC ground-truth sweep (spec §10.1, M6) — in simulation, no hardware.

Exhaustively measures BER for each modcod across SNR and prints the optimal-modcod
staircase the agent must rediscover. Run: python scripts/offline_amc_sweep.py
"""
from __future__ import annotations

import numpy as np

from gr_autopilot.control import amc_sweep
from gr_autopilot.link.numpy_sim import NumpySimBackend

LADDER = ("bpsk", "qpsk", "16qam")
TARGET_BER = 1e-3


def main() -> None:
    snrs = list(np.arange(2.0, 21.0, 2.0))
    cells = amc_sweep(NumpySimBackend(), snrs, ladder=LADDER, target_ber=TARGET_BER, n_bits=500_000)

    print(f"Offline AMC sweep (AWGN sim) — target BER <= {TARGET_BER:g}\n")
    print(f"{'Es/N0':>6} | " + " ".join(f"{m:>10}" for m in LADDER) + " | optimal")
    print("-" * 58)
    for c in cells:
        bers = " ".join(f"{c.ber[m]:>10.2e}" for m in LADDER)
        print(f"{c.snr_db:6.1f} | {bers} | {c.optimal}")

    print("\nStaircase (SNR -> optimal modcod):")
    for c in cells:
        bar = {"none": "·", "bpsk": "▁", "qpsk": "▄", "16qam": "█"}.get(c.optimal, "?")
        print(f"  {c.snr_db:5.1f} dB  {bar}  {c.optimal}")


if __name__ == "__main__":
    main()
