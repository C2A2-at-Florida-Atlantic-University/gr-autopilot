#!/usr/bin/env python3
"""Stage 3 (sim): frequency hopping evades a reactive jammer that avoidance cannot beat.

The escalation on top of Stage 2:
  1. a FIXED jammer -> the agent SENSES it and frequency-AVOIDS (Stage 2).
  2. a REACTIVE jammer (follows the link) -> avoidance FAILS: sensing sees the band clear, but a
     retuned link is still jammed (the honest limit we characterized in Stage-2 depth).
  3. the agent HOPS -> escalates the hop rate until it out-runs the jammer's reaction (BER meets
     target), then AMC restores the top modcod on the hopped link.
  4. a FAST (FPGA) reactive jammer -> no achievable hop rate evades: the honest floor of hopping.

The agent is told neither the SNR nor the jammer's reaction time; it discovers the needed hop rate
from measured BER alone. Sim only (InterferenceSimBackend); no radios.

Run:  python scripts/run_hopping_demo.py
"""
from __future__ import annotations

import argparse

from gr_autopilot.control.avoidance import FrequencyAvoidanceController
from gr_autopilot.control.hopping import FrequencyHoppingController
from gr_autopilot.link.hopping import min_evading_hop_rate_hz
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clean-snr", type=float, default=20.0)
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--channels", default="2400,2401,2402,2403,2404,2405,2406,2407",
                    help="hop set, MHz")
    ap.add_argument("--hop-rates", default="50,100,200,400,800,1600,3200",
                    help="escalating hop rates to try, Hz")
    args = ap.parse_args()
    chans = tuple(float(m) * 1e6 for m in args.channels.split(","))
    rates = tuple(float(r) for r in args.hop_rates.split(","))

    def fresh(jam):
        return InterferenceSimBackend(clean_es_n0_db=args.clean_snr, interferer=jam,
                                      center_freq_hz=chans[0])

    print(f"Stage 3 — frequency hopping vs a reactive jammer  (sim, clean SNR {args.clean_snr:.0f} dB, "
          f"target BER {args.target_ber:g})")
    print(f"hop set: {len(chans)} channels · the agent knows neither the SNR nor the jammer's reaction\n")

    fixed = Interferer(center_freq_hz=chans[0], inr_db=30.0, kind="cw", reactive=False)
    av = FrequencyAvoidanceController(fresh(fixed), chans[:5], target_ber=args.target_ber,
                                      probe_bits=8000, confirm_bits=8000).run()
    print(f"[1] FIXED jammer @ {chans[0]/1e9:.3f} GHz -> AVOIDANCE: sensed, retuned to "
          f"{(av.chosen_freq_hz or 0)/1e6:.0f} MHz, restored "
          f"{av.amc.chosen.upper() if av.amc else '-'}  (beaten={av.beaten})")

    for reaction_ms, label in [(5.0, "SLOW (software / USB latency)"), (0.2, "FAST (FPGA)")]:
        jam = Interferer(center_freq_hz=chans[0], inr_db=30.0, kind="cw", reactive=True,
                         reaction_s=reaction_ms * 1e-3)
        be = fresh(jam)
        av = FrequencyAvoidanceController(be, chans[:5], target_ber=args.target_ber,
                                          probe_bits=8000, confirm_bits=8000).run()
        be.set_condition(hop_plan=None, center_freq_hz=chans[0])
        hop = FrequencyHoppingController(be, chans, rates, target_ber=args.target_ber).run()
        need = min_evading_hop_rate_hz(jam)
        print(f"\n[2] REACTIVE jammer — {label} (reaction {reaction_ms:.1f} ms -> needs > {need:.0f} Hz):")
        print(f"    avoidance -> beaten={av.beaten}  (band sensed clear, but a retuned link is still jammed)")
        for t in hop.trajectory:
            print(f"    hop {t['hop_rate_hz']:>6.0f} Hz : BER={t['ber']:.2e}  "
                  f"[{'EVADED' if t['evaded'] else 'jammed'}]")
        if hop.evaded:
            print(f"    => HOPPING EVADES at {hop.hop_rate_hz:.0f} Hz; AMC restores "
                  f"{hop.amc.chosen.upper()} on the hopped link  (beaten={hop.beaten})")
        else:
            print("    => honest limit: no achievable hop rate out-reacts this jammer "
                  "(faster hopping / an FPGA radio needed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
