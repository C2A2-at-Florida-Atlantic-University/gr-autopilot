#!/usr/bin/env python3
"""Zadoff-Chu vs legacy acquisition floor on real PlutoSDRs.

Sweeps TX attenuation (lowering the hidden physical SNR) and, at each setting, runs the SAME
persistent link with both front-ends -- legacy (blind Gardner+Costas then correlate) and ZC
(data-aided Zadoff-Chu acquisition first) -- for BPSK and QPSK. The claim under test: ZC keeps
locking (BER near 0) at higher attenuation / lower SNR than the blind loops, and low enough to
expose the 3rd AMC rung where BPSK out-survives QPSK.

One backend, toggling sync_mode between runs, so both front-ends see the identical RF path.

Run:  python scripts/hw_zc_floor.py
      python scripts/hw_zc_floor.py --rx-gain 64 --attens 60,66,70,72,74 --trials 3
Requires GNU Radio + gr-iio and the two radios. Ends with os._exit (gr-iio teardown hangs).
"""
from __future__ import annotations

import argparse
import os
import sys

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring import metrics


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--freq", type=float, default=2.4e9)
    ap.add_argument("--rate", type=float, default=2_084_000.0)
    ap.add_argument("--sps", type=int, default=8)
    ap.add_argument("--rolloff", type=float, default=0.35)
    ap.add_argument("--rx-gain", type=float, default=70.0)
    ap.add_argument("--attens", default="50,58,64,68,70,72,74,76",
                    help="comma-separated TX attenuations dB (higher = lower SNR)")
    ap.add_argument("--nsyms", type=int, default=2000)
    ap.add_argument("--trials", type=int, default=2)
    args = ap.parse_args()

    attens = [float(a) for a in args.attens.split(",")]

    try:
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import PlutoBackend ({exc}); need GNU Radio + gr-iio", file=sys.stderr)
        return 2

    be = PlutoBackend(args.tx_uri, args.rx_uri, center_freq_hz=args.freq,
                      sample_rate=args.rate, rx_gain_db=args.rx_gain)

    print(f"ZC vs legacy floor: {args.tx_uri} -> {args.rx_uri} @ {args.freq/1e9:.3f} GHz  "
          f"sps={args.sps}  RXgain={args.rx_gain}dB  {args.trials} trials/cell")
    print(f"{'atten':>5} {'mod':>5} {'sync':>7} {'medBER':>9} {'best':>9} {'SNRest':>7} "
          f"{'lock/tone':>10}")

    def run(mod, nbits, sync_mode, atten):
        be.sync_mode = sync_mode
        be.set_condition(tx_atten_db=atten)
        bers, snrs, locks = [], [], []
        for _ in range(args.trials):
            try:
                r = be.run_link(LinkParams(modulation=mod, n_payload_bits=nbits,
                                           sps=args.sps, rolloff=args.rolloff, seed=1))
            except Exception as exc:
                bers.append(1.0); locks.append(f"ERR")
                continue
            m = metrics.compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
            bers.append(m.ber); snrs.append(m.snr_db)
            if sync_mode == "zc":
                locks.append(f"{r.meta.get('frame_sync_tone', 0):.0f}"
                             if r.meta.get("zc_locked") else "no")
            else:
                locks.append(f"{r.meta.get('frame_sync_sharpness', 0):.0f}")
        bers.sort()
        med = bers[len(bers) // 2]
        best = min(bers)
        snr = (sum(snrs) / len(snrs)) if snrs else float("nan")
        return med, best, snr, ",".join(locks)

    for atten in attens:
        for mod in ("bpsk", "qpsk"):
            nbits = args.nsyms * (1 if mod == "bpsk" else 2)
            for sync_mode in ("legacy", "zc"):
                med, best, snr, locks = run(mod, nbits, sync_mode, atten)
                print(f"{atten:>5.0f} {mod:>5} {sync_mode:>7} {med:>9.2e} {best:>9.2e} "
                      f"{snr:>6.1f}  {locks:>10}", flush=True)
        print("-" * 56, flush=True)

    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
