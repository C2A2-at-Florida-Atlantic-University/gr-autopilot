#!/usr/bin/env python3
"""Adaptive modulation AND coding (sim): the agent's ladder now includes coded modcods, and at
marginal SNRs it ADDS CODING to hold throughput where a bare modulation would fail. Two-loop
controller, SNR withheld, target BER 1e-3. The staircase climbs

    BPSK -> QPSK:r3/4 -> QPSK -> 16QAM:r3/4 -> 16QAM

with the rate-3/4 coded rungs (1.5 and 3.0 bits/sym) filling the gaps between the bare
modulation rungs. Rediscovered from measurements alone. This demo is sim, but coding also runs on
the hardware backend -- BPSK/QPSK robustly, and 16-QAM shows a coding gain with a characterized
carrier-ambiguity limit (see scripts/hw_psam_coded16.py).

Run:  python scripts/coded_amc_sim.py
"""
from __future__ import annotations

from gr_autopilot.control import TwoLoopController
from gr_autopilot.control.objective import parse_modcod, spectral_efficiency
from gr_autopilot.link.numpy_sim import NumpySimBackend

LADDER = ("bpsk", "qpsk:conv_k3_r34", "qpsk", "16qam:conv_k3_r34", "16qam")
SNRS = [8.0, 9.0, 11.0, 13.0, 15.0, 18.0]


def main() -> None:
    ctrl = TwoLoopController(NumpySimBackend(), target_ber=1e-3, knobs={}, ladder=LADDER,
                             trial_bits=8000, confirm_bits=8000, seed=0)
    print(f"adaptive modulation AND coding, ladder = {LADDER}, target BER <= 1e-3, SNR withheld\n")
    print(f"{'Es/N0':>6} | {'agent chose':>20} | {'bits/sym':>8} | note")
    for snr in SNRS:
        res = ctrl.run({"es_n0_db": snr})
        _, coding = parse_modcod(res.chosen)
        note = "coded rung (added FEC)" if coding else ""
        print(f"{snr:>5.0f}  | {res.chosen:>20} | {spectral_efficiency(res.chosen):>8.1f} | {note}")
    print("\nThe agent inserts a rate-3/4 coded modcod exactly where a bare modulation stops "
          "meeting the target -- trading a little rate for the coding gain that keeps the link up.")


if __name__ == "__main__":
    main()
