#!/usr/bin/env python3
"""Measure the PlutoSDR's live-retune latency and the minimum per-hop dwell (Stage-3 groundwork).

Frequency hopping's whole physics turns on ONE hardware number: how fast can the link change
channel and still decode? Two components, both measured here on the real radios:

  (A) command round-trip -- how long ``set_frequency()`` takes to return (the libiio attribute
      write over the USB-ethernet context). This is the floor on how often a hop can be *issued*.
  (B) per-hop dwell -- after a live retune, how much settle the receiver needs before it decodes
      cleanly (PLL relock + DMA flush + ZC re-acquire). We sweep the settle and read the BER, so
      the smallest settle with a clean BER is the real dwell floor. 1/dwell is the max hop rate.

Why it matters: a channel-following jammer is out-hopped only if the link's dwell is shorter than
the jammer's reaction latency (see scripts/run_hw_hopping.py). This
probe fixes the link side of that inequality with a measured number, not a guess.

Wiring: the normal cabled/attenuated two-Pluto path (nothing radiates). gr-iio teardown hangs, so
we end with os._exit.

Run:  python scripts/probe_retune_latency.py
      python scripts/probe_retune_latency.py --channels 2400,2402,2404,2406 --settles 0.02,0.05,0.1,0.2,0.3
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring.metrics import compute_metrics


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--tx-atten", type=float, default=20.0, help="clean link budget (dB)")
    ap.add_argument("--channels", default="2400,2402,2404,2406", help="hop-set centers, MHz")
    ap.add_argument("--settles", default="0.02,0.05,0.1,0.15,0.2,0.3",
                    help="settle_s values to sweep for the dwell floor")
    ap.add_argument("--bits", type=int, default=6000, help="payload bits per probe (QPSK)")
    ap.add_argument("--n-cmd", type=int, default=20, help="retunes to time for the command round-trip")
    args = ap.parse_args()

    try:
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import PlutoBackend ({exc})", file=sys.stderr)
        return 2

    chans = [float(m) * 1e6 for m in args.channels.split(",")]
    settles = [float(s) for s in args.settles.split(",")]
    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0,
                      rx_gain_db=args.rx_gain, sync_mode="zc")
    be.set_condition(tx_atten_db=args.tx_atten, center_freq_hz=chans[0])

    print(f"Retune-latency probe  link {args.tx_uri} -> {args.rx_uri}")
    print(f"hop set: {', '.join(f'{c/1e6:.0f}' for c in chans)} MHz   tx_atten={args.tx_atten:g} dB\n")

    # Warm up the persistent flowgraph on channel 0 (also confirms the link is alive).
    warm = be.run_link(LinkParams(modulation="qpsk", n_payload_bits=args.bits, sps=8,
                                   rolloff=0.35, seed=0))
    m0 = compute_metrics(warm.tx_bits, warm.rx_bits, warm.rx_syms, warm.ref_syms)
    print(f"warm-up on {chans[0]/1e6:.0f} MHz: BER={m0.ber:.2e}  SNR_est={m0.snr_db:.1f} dB\n")

    # (A) command round-trip: time set_frequency() on both LOs, alternating channels so each call
    # is a real retune. Private handles are fine here -- this is our own backend.
    print(f"(A) set_frequency() command round-trip over {args.n_cmd} retunes:")
    cmd_ms = []
    for i in range(args.n_cmd):
        f = chans[(i + 1) % len(chans)]
        t0 = time.perf_counter()
        be._txsink.set_frequency(int(f))
        be._rxsrc.set_frequency(int(f))
        cmd_ms.append((time.perf_counter() - t0) * 1e3)
    cmd = np.array(cmd_ms)
    print(f"    median {np.median(cmd):.2f} ms   p90 {np.percentile(cmd, 90):.2f} ms   "
          f"max {cmd.max():.2f} ms   (TX+RX LO write)\n")

    # (B) per-hop dwell floor: for each settle, retune to a FRESH channel and read the BER. The
    # smallest settle that still decodes cleanly is the dwell floor; 1/dwell bounds the hop rate.
    print("(B) per-hop dwell floor (retune to a fresh channel, sweep settle, read BER):")
    print("    settle_s   wall_s   BER        SNR_est   verdict")
    default_settle = be.settle_s
    floor = None
    for i, s in enumerate(settles):
        be.settle_s = s
        f = chans[(i + 1) % len(chans)]           # a channel different from the previous probe
        be.set_condition(center_freq_hz=f)
        t0 = time.perf_counter()
        r = be.run_link(LinkParams(modulation="qpsk", n_payload_bits=args.bits, sps=8,
                                    rolloff=0.35, seed=i + 1))
        wall = time.perf_counter() - t0
        mm = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
        clean = mm.ber <= 1e-2
        if clean and floor is None:
            floor = (s, wall)
        print(f"    {s:6.3f}    {wall:5.2f}   {mm.ber:.2e}   {mm.snr_db:5.1f} dB   "
              f"{'clean' if clean else 'DIRTY (stale DMA / no relock)'}", flush=True)
    be.settle_s = default_settle

    print()
    print("=> summary:")
    print(f"   retune command : ~{np.median(cmd):.1f} ms round-trip (issue a hop this fast)")
    if floor is not None:
        s, wall = floor
        print(f"   dwell floor    : settle >= {s:.3f} s decodes cleanly; per-hop wall ~{wall:.2f} s "
              f"=> max hop rate ~{1.0/wall:.1f} Hz at this payload")
        print(f"   a channel-following jammer is out-hopped iff its reaction latency > ~{wall:.2f} s")
    else:
        print("   dwell floor    : no swept settle decoded cleanly -- raise --settles or link budget")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
