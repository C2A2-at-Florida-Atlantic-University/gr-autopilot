#!/usr/bin/env python3
"""Stage-3 frequency hopping on HARDWARE: out-hop a channel-following jammer over the wired path.

RESEARCH USE ONLY — this keys a real HackRF transmitter. It runs over a cabled, 20 dB-attenuated
coax path; nothing radiates. Do not attach an antenna or retune onto spectrum in use; transmitting
an interfering signal over the air is illegal in most jurisdictions. See RESPONSIBLE-USE.md.

Built AROUND the bench's real limitation (measured by scripts/probe_retune_latency.py): the Pluto
link retunes its LO *live* in ~4 ms and dwells
cleanly at ~0.1 s/hop (~4 Hz), while a CLI-driven HackRF follower can only chase by killing and
restarting hackrf_transfer (~1 s). That ~200x latency asymmetry is exactly what lets the link
out-hop the follower — and it is tooling-bound, not fundamental: a lower-latency follower (a
live-retune SDR / FPGA) with reaction shorter than the link's dwell would win (the honest floor).

Three acts, all measured on the radios:

  Act 1 — FIXED spot jammer. Camping on the jammed channel is 100% jammed; hopping the same energy
          across N channels spreads the hits to ~1/N (the fixed-jammer branch of link/hopping.py).
  Act 2 — CHANNEL-FOLLOWING jammer, out-hopped. A HackRF follower chases the link but lands a dwell
          late (its ~1 s reaction > the link's dwell): the link's current channel is always clean →
          evasion. We print the measured follower latency vs the link dwell — the *why*.
  Act 3 — the honest floor. A follower fast enough to retune inside the dwell (emulated by placing
          the jammer on the link's current channel) jams every hop. Hopping cannot beat it — the
          same 1/tau limit the sim characterizes (scripts/run_hopping_demo.py).

The follower is oracle-placed (handed the link's schedule) — a best case for the jammer; a sensing
follower only adds detection latency, making evasion easier. The variable is the reaction latency.

Wiring: pluto2.tx + HackRF each through 20 dB into a splitter, combined into pluto3.rx (same path as
run_hw_interference.py). Requires GNU Radio + gr-iio and hackrf_transfer. Ends with os._exit.

Run:  python scripts/run_hw_hopping.py
      python scripts/run_hw_hopping.py --channels 2400,2402,2404,2406 --settle 0.12 --cycles 2
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from gr_autopilot.control.hw_hopping import HardwareHopProbe, follower_channel
from gr_autopilot.ledger import EditLedger
from gr_autopilot.link.hopping import HopPlan


def _fmt(res, chans) -> None:
    for r in res.records:
        tag = "JAMMED" if r.jammed else "clear "
        print(f"     hop {r.hop_index:>2}  {r.channel_hz/1e6:7.1f} MHz : BER={r.ber:.2e}  "
              f"SNR_est={r.snr_db:5.1f} dB  [{tag}]", flush=True)
    print(f"     -> jammed fraction {res.jammed_fraction:.2f}  "
          f"aggregate BER {res.aggregate_ber:.2e}  "
          f"({'EVADED (link usable)' if res.evaded else 'link degraded'})\n", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--tx-atten", type=float, default=45.0,
                    help="clean link budget (dB); 45 = Stage-2's known-good jammer/link balance")
    ap.add_argument("--channels", default="2400,2402,2404,2406", help="hop set centers, MHz")
    ap.add_argument("--settle", type=float, default=0.15, help="per-hop dwell (>= measured 0.10 floor)")
    ap.add_argument("--cycles", type=int, default=2, help="times around the hop set per scenario")
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--if-gain", type=int, default=20, help="HackRF TX VGA gain (jammer strength)")
    ap.add_argument("--bits", type=int, default=6000, help="payload bits per hop (QPSK)")
    args = ap.parse_args()

    try:
        from gr_autopilot.hardware.interferer import FollowerJammer, HackRFInterferer
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import hardware backends ({exc})", file=sys.stderr)
        return 2
    if not HackRFInterferer.available():
        print("hackrf_transfer not found on PATH; need libhackrf tools", file=sys.stderr)
        return 2

    chans = [float(m) * 1e6 for m in args.channels.split(",")]
    plan = HopPlan(tuple(chans), 1.0 / (args.settle + 0.15))   # nominal hop rate at this dwell
    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0,
                      rx_gain_db=args.rx_gain, sync_mode="zc")
    be.set_condition(tx_atten_db=args.tx_atten)
    led = EditLedger(Path(tempfile.mkdtemp()) / "hop.jsonl")

    def probe(hop_plan):
        return HardwareHopProbe(be, hop_plan, target_ber=args.target_ber, probe_bits=args.bits,
                                sps=8, rolloff=0.35, settle_s=args.settle, ledger=led)

    print(f"Stage-3 hardware hopping  link {args.tx_uri} -> {args.rx_uri}")
    print(f"hop set: {', '.join(f'{c/1e6:.0f}' for c in chans)} MHz   dwell {args.settle:g}s "
          f"(~{plan.hop_rate_hz:.1f} Hz)   target BER <= {args.target_ber:g}\n")

    # ---- Act 1: fixed spot jammer -- camp vs hop -------------------------------------------------
    jam_ch = chans[0]
    print(f"[Act 1] FIXED spot jammer on {jam_ch/1e6:.0f} MHz (one of {len(chans)} channels)")
    with HackRFInterferer(center_freq_hz=jam_ch, if_gain=args.if_gain, kind="cw"):
        print("  (a) camp on the jammed channel (no hopping):")
        _fmt(probe(HopPlan((jam_ch,), plan.hop_rate_hz)).run(cycles=args.cycles), chans)
        print(f"  (b) hop across all {len(chans)} channels (jammer stays put):")
        _fmt(probe(plan).run(cycles=args.cycles), chans)

    # ---- Act 2: channel-following jammer, out-hopped ---------------------------------------------
    print("[Act 2] CHANNEL-FOLLOWING jammer chases the link (kill/restart), lands a dwell LATE")
    with FollowerJammer(center_freq_hz=chans[-1], if_gain=args.if_gain, kind="cw") as fol:
        # calibrate the follower's real reaction latency (a couple of live retunes)
        fol.retune(chans[0]); fol.retune(chans[1])
        tau = fol.last_latency_s
        print(f"  measured follower reaction latency tau ~ {tau:.2f} s   vs link dwell "
              f"~{args.settle + 0.15:.2f} s -> link hops ~{tau/(args.settle+0.15):.1f}x faster\n")

        def chase_one_behind(t, ch, history):
            tgt = follower_channel(history, ch, dwells_behind=1)   # where the link WAS (reaction late)
            if tgt is not None:
                fol.retune(tgt)
        print("  follower one dwell behind (reaction > dwell):")
        _fmt(probe(plan).run(cycles=args.cycles, before_hop=chase_one_behind), chans)

        # ---- Act 3: the honest floor -- a follower that keeps up jams every hop ------------------
        print("[Act 3] HONEST FLOOR: a follower fast enough to retune INSIDE the dwell (reaction < dwell)")

        def keep_up(t, ch, history):
            fol.retune(ch)                                         # onto the link's current channel
        _fmt(probe(plan).run(cycles=args.cycles, before_hop=keep_up), chans)

    print("=> hardware Stage 3: hopping spreads a fixed jammer to ~1/N and OUT-HOPS a slower")
    print("   channel-follower; a follower faster than the link's dwell still wins (the 1/tau floor,")
    print("   sim fig4). The win here rests on the Pluto's live ~4 ms retune vs the HackRF CLI's ~1 s.")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
