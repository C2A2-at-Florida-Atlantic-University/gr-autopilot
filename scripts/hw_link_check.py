#!/usr/bin/env python3
"""Hardware link check (M0 on real radios): run the PlutoBackend across the modulation
ladder over the cabled PlutoSDR path and report physical-layer metrics.

This is the hardware counterpart to ``scripts/m0_sim_link.py``: it exercises the exact same
LinkBackend seam (build known frame -> TX -> RF path -> tracking receiver -> grade) but on
two real PlutoSDRs instead of a simulated channel. A clean run shows BPSK/QPSK at BER 0 and
16-QAM at a low BER at a healthy lab SNR.

Defaults match the bench inventory (config/bench.yaml): pluto2 (ip:192.168.2.1) transmits,
pluto3 (ip:192.168.3.1) receives, over the 20 dB-padded coax path at 2.4 GHz.

Run:  python scripts/hw_link_check.py
      python scripts/hw_link_check.py --tx-atten 6 --rx-gain 45

Requires GNU Radio + gr-iio and the two radios connected. gr-iio device contexts hang on
destruction, so the process ends with os._exit once results are printed.
"""
from __future__ import annotations

import argparse
import os
import sys

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring import metrics

# (modulation, payload bits) — bits chosen for a whole number of symbols in one 4096-sym frame.
LADDER = [("bpsk", 4096), ("qpsk", 7680), ("16qam", 8192)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1", help="TX Pluto IIO URI (pluto2)")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1", help="RX Pluto IIO URI (pluto3)")
    ap.add_argument("--freq", type=float, default=2.4e9, help="center frequency (Hz)")
    ap.add_argument("--rate", type=float, default=2_084_000.0, help="sample rate (Hz)")
    ap.add_argument("--sps", type=int, default=8, help="samples per symbol")
    ap.add_argument("--rolloff", type=float, default=0.35, help="RRC excess bandwidth")
    ap.add_argument("--tx-atten", type=float, default=10.0, help="TX attenuation dB (0=max power)")
    ap.add_argument("--rx-gain", type=float, default=40.0, help="RX manual gain dB")
    ap.add_argument("--trials", type=int, default=1, help="repeats per modulation")
    args = ap.parse_args()

    try:
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover - import guard
        print(f"cannot import PlutoBackend ({exc}); need GNU Radio + gr-iio", file=sys.stderr)
        return 2

    be = PlutoBackend(
        args.tx_uri, args.rx_uri,
        center_freq_hz=args.freq, sample_rate=args.rate,
        tx_atten_db=args.tx_atten, rx_gain_db=args.rx_gain,
    )
    print(f"link: {args.tx_uri} -> {args.rx_uri}  @ {args.freq/1e9:.3f} GHz  "
          f"{args.rate/1e6:.3f} Msps  sps={args.sps}  TXatten={args.tx_atten}dB RXgain={args.rx_gain}dB")
    print(f"{'mod':>6}  {'BER':>10}  {'errors':>12}  {'EVM%':>6}  {'SNRest':>7}  {'sync':>6}")

    worst_ber = 0.0
    for mod, nbits in LADDER:
        for _ in range(args.trials):
            params = LinkParams(modulation=mod, n_payload_bits=nbits, sps=args.sps,
                                rolloff=args.rolloff, seed=1)
            try:
                r = be.run_link(params)
            except Exception as exc:
                print(f"{mod:>6}  FAILED: {exc}")
                continue
            m = metrics.compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
            worst_ber = max(worst_ber, m.ber)
            print(f"{mod:>6}  {m.ber:>10.2e}  {m.n_errors:>5d}/{m.n_bits:<6d}  "
                  f"{m.evm_pct:>6.1f}  {m.snr_db:>6.1f}dB  {r.meta['frame_sync_sharpness']:>4.0f}x",
                  flush=True)

    print(f"\nworst BER across ladder: {worst_ber:.2e}")
    sys.stdout.flush()
    os._exit(0)  # gr-iio context teardown hangs; exit hard after printing


if __name__ == "__main__":
    raise SystemExit(main())
