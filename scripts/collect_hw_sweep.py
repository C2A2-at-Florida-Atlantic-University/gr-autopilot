#!/usr/bin/env python3
"""Collect the hardware AMC ground truth: BER/EVM/SNR for every modulation across a sweep of
hidden physical SNRs (TX attenuation), a few trials each. This is the staircase the two-loop
controller must rediscover -- the reference figure for the paper. Writes a JSON snapshot and
prints a table.

Run:  python scripts/collect_hw_sweep.py --out runs/hw_sweep.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring import metrics

MODS = [("bpsk", 4096), ("qpsk", 8000), ("16qam", 8000)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--attens", default="30,45,51,54,57")
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--rolloff", type=float, default=0.35)
    ap.add_argument("--sync", default="legacy", choices=["legacy", "zc"],
                    help="frame-sync front-end: legacy (blind) or zc (Zadoff-Chu acquisition)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    from gr_autopilot.link.pluto import PlutoBackend
    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0, rx_gain_db=args.rx_gain,
                      sync_mode=args.sync)
    attens = [float(a) for a in args.attens.split(",")]

    rows = []
    print(f"{'atten':>5} | " + " | ".join(f"{m:>18}" for m, _ in MODS))
    print(f"{'(dB)':>5} | " + " | ".join(f"{'BER (SNRest)':>18}" for _ in MODS))
    print("-" * (8 + 21 * len(MODS)))
    for atten in attens:
        be.set_condition(tx_atten_db=atten)
        cells = []
        for mod, nbits in MODS:
            bers, snrs, evms = [], [], []
            for _ in range(args.trials):
                r = be.run_link(LinkParams(modulation=mod, n_payload_bits=nbits, sps=8,
                                           rolloff=args.rolloff, seed=1))
                m = metrics.compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
                bers.append(m.ber); snrs.append(m.snr_db); evms.append(m.evm_pct)
            med_ber = statistics.median(bers)
            row = {"atten_db": atten, "modulation": mod, "ber_median": med_ber,
                   "ber_all": bers, "snr_db_median": statistics.median(snrs),
                   "evm_pct_median": statistics.median(evms)}
            rows.append(row)
            cells.append(f"{med_ber:.1e} ({statistics.median(snrs):4.1f})")
        print(f"{atten:>5.0f} | " + " | ".join(f"{c:>18}" for c in cells), flush=True)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"config": {"attens": attens, "rx_gain_db": args.rx_gain, "trials": args.trials,
                        "rolloff": args.rolloff, "sample_rate": 2_084_000, "sps": 8,
                        "freq_hz": 2.4e9, "path": "cabled, 20 dB pad", "sync": args.sync},
             "rows": rows}, indent=2))
        print(f"\nwrote {out}")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
