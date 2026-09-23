#!/usr/bin/env python3
"""The autonomous mission on HARDWARE: one agent adapts across changing conditions on the radios.

The sim mission (`scripts/run_autonomous_mission.py`) runs the whole three-stage arc behind the
`LinkBackend` seam. This runs it on two PlutoSDRs + a HackRF over the wired path, driven by the SAME
`AutonomousAgent` over the SAME MCP tools — only the backend is real. The framework applies a physical
timeline (it sets the hidden TX attenuation and turns the jammer on/off); the agent is told none of
it and adapts from framework-graded BER alone.

  Epoch A — clean, low attenuation .............. agent climbs the AMC ladder (→ 16-QAM/QPSK)
  Epoch B — attenuation raised (hidden SNR drop)  agent drops the ladder (→ QPSK/BPSK)
  Epoch C — a HackRF jammer appears on-channel .. agent senses it and retunes to a clear channel
  Epoch D — hop coda (optional, --with-hop) ..... agent set_hop_plan; the HoppingBackend physically
            cycles the Plutos and out-hops a turn-based follower (the Result-9 mechanism), proving
            set_hop_plan works end-to-end on radios.

Honest scope: A-C are fully autonomous (real AMC + real energy-detection avoidance). The Stage-3
*reactive-sensing* trigger (a jammer that reads clear yet still jams) is NOT reproducible with an
always-on HackRF and is out of reach here (needs a full-duplex jammer) — so the
hop coda demonstrates the physical hop/evasion, not an autonomous "discover I must hop" step.

Wiring: pluto2.tx + HackRF each through 20 dB into a splitter, combined into pluto3.rx. Ends with
os._exit (gr-iio teardown hangs).

Run:  python scripts/run_hw_mission.py
      python scripts/run_hw_mission.py --with-hop --attens 38,57,45
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from gr_autopilot.control.mission import AutonomousAgent
from gr_autopilot.link.hopping import HopPlan
from gr_autopilot.link.hopping_backend import HoppingBackend
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer


class Trace:
    def __init__(self):
        self.lines = []

    def __call__(self, line=""):
        self.lines.append(line)
        print(line, flush=True)


def _epoch(say, agent, svc, label, desc, atten, jammer_ctx=None):
    say(f"[epoch {label}] framework: {desc}  (tx_atten={atten:g} dB, hidden)")
    svc.set_channel(tx_atten_db=float(atten))
    r = agent.adapt(label)
    mod = (r.chosen_modcod or "NO LINK").upper()
    verdict = "meets" if r.meets else "FAILS"
    say(f"  => {mod} ({r.bits_per_sym} b/sym) on {r.center_freq_hz/1e6:.0f} MHz — "
        f"BER={r.ber:.2e} [{verdict}], {r.runs} link trials\n")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--candidates", default="2400,2402,2404,2406", help="avoid/hop channel set, MHz")
    ap.add_argument("--attens", default="38,57,45", help="TX atten (dB) for epochs A,B,C (hidden SNR)")
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--confirm-bits", type=int, default=8000, help="payload bits per link trial")
    ap.add_argument("--if-gain", type=int, default=20, help="HackRF jammer strength")
    ap.add_argument("--with-hop", action="store_true", help="add the Stage-3 hop coda (epoch D)")
    ap.add_argument("--settle", type=float, default=0.15, help="per-hop dwell for the coda")
    args = ap.parse_args()

    try:
        from gr_autopilot.hardware.interferer import FollowerJammer, HackRFInterferer
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import hardware backends ({exc})", file=sys.stderr)
        return 2
    if not HackRFInterferer.available():
        print("hackrf_transfer not found on PATH", file=sys.stderr)
        return 2

    cands = [float(m) * 1e6 for m in args.candidates.split(",")]
    attens = [float(a) for a in args.attens.split(",")]
    workdir = Path(tempfile.mkdtemp(prefix="gr_autopilot_hwmission_"))
    pluto = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0,
                         rx_gain_db=args.rx_gain, sync_mode="zc")
    be = HoppingBackend(pluto)                       # transparent until a hop plan is set (coda)
    svc = AutopilotService(backend=be, channel={"tx_atten_db": attens[0]},
                           ledger_path=workdir / "session.jsonl")
    srv = StdioMCPServer(svc)
    cli = InProcessClient(srv)
    say = Trace()

    say(f"Autonomous mission on HARDWARE  link {args.tx_uri} -> {args.rx_uri}  "
        f"({len(cli.list_tools())} MCP tools)")
    say("one agent, changing hidden conditions on the real radios; never told the SNR or the "
        "jammer — it measures and adapts\n")

    agent = AutonomousAgent(cli, cands, target_ber=args.target_ber, confirm_bits=args.confirm_bits,
                            hop_rates=[1.0 / (args.settle + 0.15)], bo_budget=6, say=say)
    agent.setup()

    results = []
    results.append(_epoch(say, agent, svc, "A", "clean link, strong signal", attens[0]))
    results.append(_epoch(say, agent, svc, "B", "attenuation raised — hidden SNR drop", attens[1]))

    # Epoch C: a HackRF jammer appears on the agent's current channel; it senses and avoids.
    jam_ch = be.center_freq_hz or cands[0]
    say(f"[epoch C] framework: a HackRF jammer switches on at {jam_ch/1e6:.0f} MHz "
        f"(location + SNR withheld)  (tx_atten={attens[2]:g} dB)")
    svc.set_channel(tx_atten_db=attens[2])
    with HackRFInterferer(center_freq_hz=jam_ch, if_gain=args.if_gain, kind="cw"):
        r = agent.adapt("C")
    mod = (r.chosen_modcod or "NO LINK").upper()
    say(f"  => {mod} ({r.bits_per_sym} b/sym) on {r.center_freq_hz/1e6:.0f} MHz — "
        f"BER={r.ber:.2e} [{'meets' if r.meets else 'FAILS'}], {r.runs} link trials\n")
    results.append(r)

    # Epoch D (optional): the hop coda — a DIRECT physical demonstration that set_hop_plan out-runs a
    # channel-following jammer on the radios (the Result-9 turn-based-follower mechanism). Not an
    # autonomous reactive-sensing discovery (that trigger needs a full-duplex jammer, out of reach).
    if args.with_hop:
        from gr_autopilot.control.mission import EpochResult
        from gr_autopilot.flowgraph import FlowgraphSpec
        link_ch = agent.center_freq          # the channel the agent is actually on (from epoch C)
        rate = 1.0 / (args.settle + 0.15)
        say(f"[epoch D] framework: the jammer now FOLLOWS the link; the agent hops to out-run it "
            f"(turn-based follower)  (tx_atten={attens[2]:g} dB)")
        svc.set_channel(tx_atten_db=attens[2])
        pluto.settle_s = args.settle
        state = {"prev": None}
        with FollowerJammer(center_freq_hz=link_ch, if_gain=args.if_gain, kind="cw") as fol:
            # (1) static link: the follower sits on the link's channel -> jammed
            cli.call("clear_hop_plan")
            cli.call("set_center_freq", center_freq_hz=link_ch)
            cli.call("build_flowgraph", spec=FlowgraphSpec.link("qpsk").to_dict())
            cli.call("run_flowgraph", n_bits=args.confirm_bits)
            m_static = cli.call("get_metrics")
            say(f"  static link on {link_ch/1e6:.0f} MHz, follower on it: BER={m_static['BER']:.2e} "
                f"({'jammed' if m_static['BER'] > args.target_ber else 'clear'})")

            # (2) hop: the agent sets a hop plan; the HoppingBackend cycles the Plutos while the
            # follower is kept a dwell behind (framework hook). Then climb the ladder on the evaded link.
            def follower(channel):
                if state["prev"] is not None:
                    fol.retune(state["prev"])          # jam where the link just was (a dwell behind)
                state["prev"] = channel

            be.on_dwell = follower
            cli.call("set_hop_plan", channels_hz=cands, hop_rate_hz=rate)
            best = agent._amc_search()                 # ladder search over the hopped link, over MCP
            be.on_dwell = None
            cli.call("clear_hop_plan")

        if best:
            say(f"  hopping {len(cands)} channels out-runs the follower: {best['modcod'].upper()} "
                f"BER={best['ber']:.2e} [meets]\n")
            r = EpochResult("D", "hop → evaded (physical)", best["modcod"], link_ch, rate,
                            best["ber"], best["snr"], True, 0)
        else:
            say("  hopping did not restore the link at this dwell\n")
            r = EpochResult("D", "hop → not restored", None, link_ch, rate, 1.0, 0.0, False, 0)
        results.append(r)

    say("=== hardware mission summary ===")
    for r in results:
        mod = (r.chosen_modcod or "none").upper()
        say(f"  epoch {r.label}: {mod:6} ({r.bits_per_sym} b/sym) on {r.center_freq_hz/1e6:.0f} MHz  "
            f"— {r.action}")
    kept = sum(1 for r in results if r.meets)
    say(f"\n=> {kept}/{len(results)} epochs held the link at target on the real radios, "
        f"driven end-to-end over the MCP tool surface. ~{srv.token_stats()['total']} tool tokens.")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
