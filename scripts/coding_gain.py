#!/usr/bin/env python3
"""Coding-gain demo (sim, no radios): the "C" in AMC. Compare uncoded BPSK against the framework
convolutional code (K=3, rate 1/2, hard-decision Viterbi) over AWGN, at the SAME energy per info
bit (coded symbols carry Es = rate*Eb). Shows where FEC helps -- the tool the agent will trade
against modulation order once coded AMC is wired into the controller.

Run:  python scripts/coding_gain.py
"""
from __future__ import annotations

import numpy as np

from gr_autopilot.coding import CODE_RATE, conv_encode, viterbi_decode


def main() -> None:
    rng = np.random.default_rng(1)
    n = 40_000   # Viterbi is a Python loop; this keeps the demo a few seconds
    print(f"convolutional K=3 rate-{CODE_RATE:g} hard-decision Viterbi over AWGN ({n} info bits)")
    print(f"{'Eb/N0':>6} | {'uncoded BPSK':>13} | {'coded':>13} | {'gain':>6}")
    for ebn0_db in [2, 3, 4, 5, 6, 7]:
        ebn0 = 10 ** (ebn0_db / 10.0)
        sigma = np.sqrt(1.0 / (2 * ebn0))
        bits = rng.integers(0, 2, n)
        ber_u = float(np.mean(bits != ((1 - 2 * bits) + sigma * rng.standard_normal(n) < 0)))
        coded = conv_encode(bits)
        rxc = (1 - 2 * coded) * np.sqrt(CODE_RATE) + sigma * rng.standard_normal(coded.size)
        ber_c = float(np.mean(bits != viterbi_decode((rxc < 0).astype(int), n)))
        print(f"{ebn0_db:>4} dB | {ber_u:>13.2e} | {ber_c:>13.2e} | {ber_u / max(ber_c, 1e-9):>5.1f}x")
    print("\nHard-decision K=3 has a threshold (~4 dB) below which it does not help; above it the "
          "gain grows with Eb/N0 (>10x by 7 dB). A longer code or soft decisions would extend the "
          "gain to lower SNR.")


if __name__ == "__main__":
    main()
