#!/usr/bin/env python3
"""Stage-2 interference demo on HARDWARE: the agent beats a jammer by frequency avoidance.

RESEARCH USE ONLY — this keys a real HackRF transmitter. It runs over a cabled, 20 dB-attenuated
coax path; nothing radiates. Do not attach an antenna or retune onto spectrum in use; transmitting
an interfering signal over the air is illegal in most jurisdictions. See RESPONSIBLE-USE.md.

A HackRF One jams the link at a fixed (hidden) frequency over the wired splitter path. The
controller is told neither the SNR nor where the jammer is; it probes the current channel, finds
it broken, scans candidate center frequencies for a clean one, retunes there, and restores the
link with AMC. Same FrequencyAvoidanceController as tests/test_avoidance.py -- sim there, radios
here, unchanged.

Wiring: pluto2.tx + HackRF each through 20 dB into a 2-way splitter, combined into pluto3.rx.
Requires GNU Radio + gr-iio (Plutos) and hackrf_transfer (HackRF). Ends with os._exit (gr-iio
teardown hangs).

Run:  python scripts/run_hw_interference.py
      python scripts/run_hw_interference.py --jammer-freq 2.4e9 --candidates 2400,2401,2405,2410,2420
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from gr_autopilot.control.avoidance import FrequencyAvoidanceController
from gr_autopilot.ledger import EditLedger


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--jammer-freq", type=float, default=2.400e9, help="HackRF LO (Hz), hidden")
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--tx-atten", type=float, default=45.0)
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--if-gain", type=int, default=20, help="HackRF TX VGA gain (jammer strength)")
    ap.add_argument("--jammer-kind", default="cw", choices=["cw", "noise"],
                    help="cw tone or band-limited noise (partial-band)")
    ap.add_argument("--jammer-bw", type=float, default=1.0e6, help="noise jammer bandwidth (Hz)")
    ap.add_argument("--candidates", default="2400,2401,2405,2410,2420",
                    help="link center frequencies to consider, MHz (first = current/jammed)")
    ap.add_argument("--blind", action="store_true",
                    help="disable the monitor: blind-probe each channel with a full link trial")
    args = ap.parse_args()

    try:
        from gr_autopilot.hardware.interferer import HackRFInterferer
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import hardware backends ({exc})", file=sys.stderr)
        return 2
    if not HackRFInterferer.available():
        print("hackrf_transfer not found on PATH; need libhackrf tools", file=sys.stderr)
        return 2

    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0,
                      rx_gain_db=args.rx_gain, sync_mode="zc")
    be.set_condition(tx_atten_db=args.tx_atten)
    candidates = [float(m) * 1e6 for m in args.candidates.split(",")]
    led = EditLedger(Path(tempfile.mkdtemp()) / "itf.jsonl")

    print(f"Stage-2 interference demo  link {args.tx_uri} -> {args.rx_uri}")
    print(f"HackRF jammer ON at {args.jammer_freq/1e9:.4f} GHz (location + SNR withheld), "
          f"target BER <= {args.target_ber:g}\n")

    # start() now raises if the HackRF fails to come up, so we never measure a "clean band" against
    # a jammer that never radiated; re-check it survived the whole run before trusting the verdict.
    with HackRFInterferer(center_freq_hz=args.jammer_freq, if_gain=args.if_gain,
                          kind=args.jammer_kind, bandwidth_hz=args.jammer_bw) as itf:
        ctrl = FrequencyAvoidanceController(
            be, candidates, target_ber=args.target_ber, probe_bits=8000, confirm_bits=8000,
            sps=8, rolloff=0.35, ledger=led, use_monitor=not args.blind)
        res = ctrl.run()
        jammer_alive = itf.running()

    if not jammer_alive:
        print("\n!! the HackRF jammer DIED mid-experiment — a 'clean' verdict here is meaningless; "
              "results are NOT trustworthy. Rerun with a working jammer.", file=sys.stderr)
        sys.stderr.flush()
        os._exit(3)

    if res.mode == "sensed":
        print("monitor: sensed spectrum (link silent -> received power in the link band):")
        for o in res.occupancy:
            tag = "JAMMER" if o["occupied"] else "clear"
            print(f"  {o['center_freq_hz']/1e6:8.3f} MHz : {o['power_db']:+6.1f} dB over noise  "
                  f"[{tag}]", flush=True)
    else:
        print("blind: probe each channel with a full link trial:")
        for s in res.scan:
            tag = "CLEAN" if s["clean"] else "jammed"
            print(f"  {s['center_freq_hz']/1e6:8.3f} MHz : BER={s['ber']:.2e}  "
                  f"SNR_est={s['snr_db']:5.1f} dB   [{tag}]", flush=True)

    if res.chosen_freq_hz is not None and res.beaten:
        verb = "sensed and avoided" if res.mode == "sensed" else "avoided"
        print(f"\n=> agent {verb} the jammer: retuned {res.jammed_freq_hz/1e6:.3f} MHz "
              f"-> {res.chosen_freq_hz/1e6:.3f} MHz, restored link at "
              f"{res.amc.chosen.upper()} (max spectral efficiency meeting BER)")
        for t in res.amc.trajectory:
            meet = "meets" if t["ber"] <= args.target_ber else "FAILS"
            print(f"     AMC {t['modulation']:>6}: BER={t['ber']:.2e}  [{meet}]")
    elif res.chosen_freq_hz is not None and not res.beaten:
        print(f"\n=> retuned to a sensed-clear channel ({res.chosen_freq_hz/1e6:.3f} MHz) but the "
              f"link is STILL jammed -> a reactive jammer energy detection can't see")
    else:
        print("\n=> no clear channel found in the candidate set (jammer too wideband)")

    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
