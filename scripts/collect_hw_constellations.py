#!/usr/bin/env python3
"""Capture the recovered RX constellation for each modulation on hardware (post timing/carrier
recovery, framework-aligned), render a compact ASCII scatter for docs, and save the points to
JSON for a real plot. A clean visual that the agent's receiver actually demodulates the link.

Run:  python scripts/collect_hw_constellations.py --atten 40 --out runs/hw_constellations.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring import metrics

MODS = [("bpsk", 4096), ("qpsk", 8000), ("16qam", 8000)]


def ascii_scatter(syms: np.ndarray, size: int = 17) -> list[str]:
    s = syms / (np.sqrt(np.mean(np.abs(syms) ** 2)) + 1e-12)
    lim = np.percentile(np.abs(np.concatenate([s.real, s.imag])), 99.5) + 1e-9
    grid = np.zeros((size, size), dtype=int)
    for z in s[:6000]:
        c = int(round(size // 2 + z.real / lim * (size // 2 - 1)))
        r = int(round(size // 2 - z.imag / lim * (size // 2 - 1)))
        if 0 <= r < size and 0 <= c < size:
            grid[r][c] += 1
    ramp = " .:-=+o*#@"
    mx = grid.max() or 1
    return ["".join(ramp[min(len(ramp) - 1, int(v / mx * (len(ramp) - 1) + 0.999))] for v in row)
            for row in grid]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--atten", type=float, default=40.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    from gr_autopilot.link.pluto import PlutoBackend
    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0)
    be.set_condition(tx_atten_db=args.atten)

    out_rows = []
    for mod, nbits in MODS:
        r = be.run_link(LinkParams(modulation=mod, n_payload_bits=nbits, sps=8, rolloff=0.35, seed=1))
        m = metrics.compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
        art = ascii_scatter(np.asarray(r.rx_syms))
        print(f"\n{mod.upper()}   BER={m.ber:.1e}  EVM={m.evm_pct:.1f}%  SNR={m.snr_db:.1f} dB")
        for line in art:
            print("   " + line)
        pts = np.asarray(r.rx_syms)[:400]
        out_rows.append({"modulation": mod, "ber": m.ber, "evm_pct": m.evm_pct, "snr_db": m.snr_db,
                         "ascii": art, "points": [[float(z.real), float(z.imag)] for z in pts]})

    if args.out:
        p = Path(args.out); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"atten_db": args.atten, "constellations": out_rows}, indent=2))
        print(f"\nwrote {args.out}")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
