#!/usr/bin/env python3
"""AMC rediscovery on HARDWARE (spec §4/§10.1): the two-loop controller drives the PlutoSDR
link. At each hidden physical SNR (set by TX attenuation, never revealed to the controller),
it walks the modulation ladder and keeps the highest-spectral-efficiency modcod whose measured
BER meets the target -- the same reference loop as scripts/run_reference_loop.py, but over the
real cabled radios instead of a simulated channel.

The controller is NOT told the attenuation or the measured SNR; it decides only from BER.

Run:  python scripts/run_hw_amc.py
      python scripts/run_hw_amc.py --attens 35,52,57
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from gr_autopilot.control import TwoLoopController
from gr_autopilot.ledger import EditLedger


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--attens", default="35,52,57",
                    help="comma-separated hidden TX attenuations (dB), one experiment each")
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--confirm-bits", type=int, default=8000)
    ap.add_argument("--sync", default="legacy", choices=["legacy", "zc"],
                    help="frame-sync front-end: legacy (blind) or zc (Zadoff-Chu acquisition, "
                         "reaches the low-SNR BPSK rung the blind loops cannot)")
    args = ap.parse_args()

    try:
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import PlutoBackend ({exc}); need GNU Radio + gr-iio", file=sys.stderr)
        return 2

    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0, rx_gain_db=args.rx_gain,
                      sync_mode=args.sync)
    attens = [float(a) for a in args.attens.split(",")]

    print(f"AMC on hardware  {args.tx_uri} -> {args.rx_uri}  target BER <= {args.target_ber:g}")
    for atten in attens:
        be.set_condition(tx_atten_db=atten)  # the hidden physical condition
        led = EditLedger(Path(tempfile.mkdtemp()) / "hw.jsonl")
        ctrl = TwoLoopController(
            be, target_ber=args.target_ber, ladder=("bpsk", "qpsk", "16qam"),
            # full two-loop: the default inner loop (phase-correction BO) runs on each rung. It is
            # a small but real gain on the 16-QAM rung at marginal SNR, measured on the bench;
            # pass knobs={} to disable it for outer-loop-only AMC.
            confirm_bits=args.confirm_bits, ledger=led, seed=0, sps=8, rolloff=0.35,
        )
        res = ctrl.run({})  # empty: the physical condition lives on the backend, hidden
        print(f"\n=== TX atten {atten:.0f} dB (SNR withheld) -> chose {res.chosen.upper()} "
              f"in {res.iterations} iterations ===")
        for t in res.trajectory:
            meet = "meets" if t["ber"] <= args.target_ber else "FAILS"
            print(f"    {t['modulation']:>6}: BER={t['ber']:.2e}  EVM={t['evm_pct']:5.1f}%  "
                  f"SNR_est={t['snr_db']:5.1f} dB  [{meet} target]")

    sys.stdout.flush()
    os._exit(0)  # gr-iio teardown hangs; exit hard after printing


if __name__ == "__main__":
    raise SystemExit(main())
