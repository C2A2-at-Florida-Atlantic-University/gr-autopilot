#!/usr/bin/env python3
"""Reference two-loop controller (spec §4, M4) — AMC rediscovery in simulation, no hardware.

At three hidden SNR settings the controller searches modcods (outer loop) and BO-tunes
continuous knobs (inner loop), converging to the optimal modulation WITHOUT being told the
SNR. Prints the choice, the per-rung measurements, and the edit-ledger table.

Run: python scripts/run_reference_loop.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from gr_autopilot.control import TwoLoopController
from gr_autopilot.ledger import EditLedger
from gr_autopilot.link.numpy_sim import NumpySimBackend

SETTINGS = [("low", 8.0), ("mid", 12.0), ("high", 18.0)]
EXPECTED = {"low": "bpsk", "mid": "qpsk", "high": "16qam"}


def main() -> None:
    for label, es_n0 in SETTINGS:
        led = EditLedger(Path(tempfile.mkdtemp()) / f"{label}.jsonl")
        ctrl = TwoLoopController(NumpySimBackend(), target_ber=1e-3, bo_budget=10,
                                trial_bits=50_000, confirm_bits=500_000, ledger=led, seed=0)
        res = ctrl.run({"es_n0_db": es_n0})  # es_n0 is the hidden channel; the controller never reads it

        ok = "OK" if res.chosen == EXPECTED[label] else f"!= expected {EXPECTED[label]}"
        print(f"\n=== {label} SNR ({es_n0:.0f} dB, withheld) -> chose {res.chosen.upper()}  [{ok}] "
              f"in {res.iterations} iterations ===")
        for t in res.trajectory:
            print(f"    {t['modulation']:>6}: BER={t['ber']:.2e}  EVM={t['evm_pct']:5.1f}%  "
                  f"SNR_est={t['snr_db']:5.1f} dB")
        print("  edit ledger:")
        print("    " + led.compact_table().replace("\n", "\n    "))


if __name__ == "__main__":
    main()
