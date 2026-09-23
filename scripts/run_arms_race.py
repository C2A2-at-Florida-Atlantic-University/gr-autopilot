#!/usr/bin/env python3
"""The arms race: a co-evolving adversary vs the adaptive agent (Stage 3+, sim).

The agent and a jammer escalate against each other, each move forcing the next: a fixed jammer is
AVOIDED; it becomes a channel-FOLLOWER; the agent HOPS faster than it can retune; the jammer SHRINKS
its reaction latency to catch that rate; the agent hops faster still -- until a fixed point set by the
jammer's reaction FLOOR (its hardware) versus the agent's fastest achievable hop:

  * a beatable jammer (a slow reaction floor) is out-hopped -> the AGENT wins with a stable hopped link;
  * a fast-enough jammer (a low floor, an FPGA follower) out-reacts every achievable hop -> the JAMMER
    wins -- the 1/tau limit the hopping demo characterizes, now reached by escalation rather than assumed.

Same AutonomousAgent that drives the MCP tools (AMC -> avoid -> hop); only the jammer is new
(gr_autopilot/control/arms_race.py). Pure sim, no radios.

Run:  python scripts/run_arms_race.py
      python scripts/run_arms_race.py --floor-ms 5 --hop-rates 50,100,200,400,800
"""
from __future__ import annotations

import argparse
import sys

from gr_autopilot.control.arms_race import AdaptiveJammer, run_arms_race
from gr_autopilot.control.mission import AutonomousAgent
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer


def one_race(label, floor_ms, cands, rates, inr, clean, target, bits):
    be = InterferenceSimBackend(clean_es_n0_db=clean, center_freq_hz=cands[0])
    svc = AutopilotService(backend=be, channel={"clean_es_n0_db": clean})
    cli = InProcessClient(StdioMCPServer(svc))
    agent = AutonomousAgent(cli, cands, target_ber=target, hop_rates=rates, confirm_bits=bits)
    agent.setup()
    jam = AdaptiveJammer(start_freq_hz=cands[0], inr_db=inr,
                         reaction_start_s=20e-3, reaction_floor_s=floor_ms * 1e-3)
    print(f"\n===== {label}: jammer reaction floor {floor_ms:g} ms "
          f"(cannot catch hops above {1000.0/floor_ms:.0f} Hz); agent max hop {max(rates):.0f} Hz =====")
    res = run_arms_race(agent, svc, jam, clean_es_n0_db=clean, say=lambda *a: print(*a))
    verdict = (f"stable hopped link at {res.final_hop_rate_hz:.0f} Hz" if res.winner == "agent"
               else "the agent's fastest hop is out-reacted")
    print(f"  >>> {res.winner.upper()} WINS in {res.rounds} rounds — {verdict}\n")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--floor-ms", type=float, default=None,
                    help="jammer reaction floor (ms); default runs BOTH a beatable and a fast jammer")
    ap.add_argument("--channels", default="2400,2401,2402,2403", help="hop/avoid channel set, MHz")
    ap.add_argument("--hop-rates", default="50,100,200,400,800", help="agent hop rates to escalate, Hz")
    ap.add_argument("--inr-db", type=float, default=22.0, help="jammer strength (INR at full overlap)")
    ap.add_argument("--clean-snr", type=float, default=20.0, help="clean-channel Es/N0 (dB)")
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--bits", type=int, default=20000)
    args = ap.parse_args()

    cands = [float(m) * 1e6 for m in args.channels.split(",")]
    rates = [float(r) for r in args.hop_rates.split(",")]
    print("Arms race — a co-evolving jammer vs the adaptive agent")
    print("the agent is told neither the SNR nor the jammer; the jammer only reacts to which "
          "countermeasure it observes")

    if args.floor_ms is not None:
        one_race("custom", args.floor_ms, cands, rates, args.inr_db, args.clean_snr,
                 args.target_ber, args.bits)
    else:
        one_race("BEATABLE jammer", 5.0, cands, rates, args.inr_db, args.clean_snr,
                 args.target_ber, args.bits)
        one_race("FAST FPGA jammer", 0.4, cands, rates, args.inr_db, args.clean_snr,
                 args.target_ber, args.bits)
        print("=> who wins is set by the jammer's reaction FLOOR vs the agent's fastest hop — the "
              "1/τ limit,\n   now the fixed point of an escalation rather than an assumption.")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
