#!/usr/bin/env python3
"""Band-envelope sweep (sim) — where does ZC acquisition hold across RF bands, and why.

The ZC front-end tolerates a fixed CFO RANGE in Hz (cfo_max cyc/sym × symbol rate); the offset between
two free-running Pluto LOs is ppm·f_c, which grows with the band. So there's a band CEILING above which
acquisition fails regardless of SNR. This sweeps the real sync.acquire over a synthetic pulse-shaped
channel across bands (and ppm) and prints the operating envelope + the closed-form ceiling. No radios.

Run:  python scripts/band_sweep_sim.py
      python scripts/band_sweep_sim.py --ppm 25 --esn0 15
"""
from __future__ import annotations

import argparse
import sys

from gr_autopilot.link import band

BANDS_GHZ = [0.4, 0.7, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4, 3.0, 3.5]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ppm", type=float, nargs="+", default=[10.0, 25.0, 40.0],
                    help="differential LO error(s) to sweep, ppm")
    ap.add_argument("--esn0", type=float, default=15.0, help="Es/N0 (dB) — high, to isolate the CFO effect")
    ap.add_argument("--trials", type=int, default=12)
    args = ap.parse_args()

    bands = [g * 1e9 for g in BANDS_GHZ]
    Rs = band.symbol_rate_hz()
    rng_hz = band.acquisition_range_hz(Rs)
    print(f"ZC acquisition range = cfo_max·Rs = 0.15 × {Rs/1e3:.1f} ksym/s = ±{rng_hz/1e3:.1f} kHz "
          f"(symbol rate {Rs/1e3:.1f} ksym/s, sps={band.BENCH_SPS})\n")

    for ppm in args.ppm:
        ceil = band.band_ceiling_hz(ppm, Rs)
        print(f"=== differential LO error {ppm:g} ppm  →  predicted ceiling {ceil/1e9:.2f} GHz "
              f"(CFO crosses ±{rng_hz/1e3:.0f} kHz there) ===")
        print(f"  {'band':>7} {'CFO':>10} {'cyc/sym':>8} {'in-range':>9} {'acq rate':>9} {'med BER':>9}")
        rows = band.band_sweep(bands, ppm, trials=args.trials, es_n0_db=args.esn0)
        for r in rows:
            flag = "" if r["in_range"] else "  ← over"
            print(f"  {r['center_freq_hz']/1e9:6.2f}G {r['cfo_hz']/1e3:8.1f}k {r['cfo_cyc_sym']:8.3f} "
                  f"{str(r['in_range']):>9} {100*r['acq_rate']:7.0f}%  {r['median_ber']:8.2e}{flag}", flush=True)
        print()

    print("Reading it: acquisition is band-FLAT below the ceiling and collapses above it — a CFO limit,\n"
          "not an SNR one. Mitigations (all one-knob): raise cfo_max, raise the symbol rate (both widen the\n"
          "Hz range), or discipline the LO (lower ppm via xo_correction / a shared reference). The wired-\n"
          "hardware sweep (scripts/band_sweep_hw.py) measures the REAL differential ppm and confirms the knee.")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
