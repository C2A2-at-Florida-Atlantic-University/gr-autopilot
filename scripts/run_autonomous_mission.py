#!/usr/bin/env python3
"""A sustained AUTONOMOUS mission over the MCP tool surface — the three stages in one run.

The framework applies a timeline of CHANGING hidden conditions (SNR shifts, a jammer appears, the
jammer starts following, the threat clears). A single autonomous agent — driven only by
framework-graded measurements over the MCP tools, never told the condition — adapts at each step:
climbs/drops the AMC ladder, senses-and-avoids a jammer, and hops to out-run a reactive one. This is
the whole thesis in one unattended session, and it emits a transcript the paper can quote.

The agent here is the deterministic reference policy (``control/mission.AutonomousAgent``); it calls
the EXACT tools a real LLM would. To put an actual LLM in this seam, point an MCP client at the same
server:  ``claude mcp add gr-autopilot -- python -m gr_autopilot.mcp_server``  and give it the
``link_adaptive`` goal — the tool surface (sense_spectrum / set_center_freq / set_hop_plan) is now
complete for all three stages.

Run:  python scripts/run_autonomous_mission.py
      python scripts/run_autonomous_mission.py --transcript runs/mission.md
      python scripts/run_autonomous_mission.py --serve 8080   # watch it live on the dashboard
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from gr_autopilot.control.mission import AutonomousAgent
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.telemetry import TelemetryWriter
from gr_autopilot.telemetry.server import serve
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer


class Trace:
    def __init__(self, quiet=False):
        self.lines: list[str] = []
        self.quiet = quiet

    def __call__(self, line: str = "") -> None:
        self.lines.append(line)
        if not self.quiet:
            print(line, flush=True)


def mission_timeline(candidates):
    """The framework's scripted sequence of hidden conditions (label, description, channel dict).
    The agent is told none of this — it re-measures and re-adapts each time."""
    c0 = candidates[0]
    return [
        ("A", "clean link, strong signal",
         {"clean_es_n0_db": 20.0, "interferer": None}),
        ("B", "SNR drops (fading / added path loss)",
         {"clean_es_n0_db": 6.0, "interferer": None}),
        ("C", "a fixed jammer appears on the link's channel",
         {"clean_es_n0_db": 18.0, "interferer": Interferer(c0, inr_db=25.0, kind="cw")}),
        ("D", "the jammer starts FOLLOWING the link (reactive, tau=5 ms)",
         {"clean_es_n0_db": 18.0,
          "interferer": Interferer(2.4e9, inr_db=20.0, kind="cw", reactive=True, reaction_s=5e-3)}),
        ("E", "the threat clears",
         {"clean_es_n0_db": 20.0, "interferer": None}),
    ]


def drive_mission(agent, svc, timeline, say) -> list:
    results = []
    for label, desc, cond in timeline:
        say(f"[epoch {label}] framework: {desc}")
        svc.set_channel(**cond)
        r = agent.adapt(label)
        mod = (r.chosen_modcod or "NO LINK").upper()
        hop = f", hopping @ {r.hop_rate_hz:.0f} Hz" if r.hop_rate_hz else ""
        verdict = "meets" if r.meets else "FAILS"
        say(f"  => {mod} ({r.bits_per_sym} b/sym) on {r.center_freq_hz/1e6:.0f} MHz{hop} — "
            f"BER={r.ber:.2e} [{verdict}], {r.runs} link trials\n")
        results.append(r)
    return results


def _summary(results, say, tokens=None):
    say("=== mission summary — one autonomous agent, five conditions, driven over MCP ===")
    say(f"  {'epoch':6} {'chosen':7} {'b/sym':5} {'freq':9} {'countermeasure'}")
    for r in results:
        mod = (r.chosen_modcod or "none").upper()
        say(f"  {r.label:6} {mod:7} {r.bits_per_sym:^5} {r.center_freq_hz/1e6:6.0f}MHz  {r.action}")
    total_runs = sum(r.runs for r in results)
    adapted = sum(1 for r in results if r.meets)
    say(f"\n  {adapted}/{len(results)} epochs kept the link at target; {total_runs} physical link "
        f"trials across the whole mission.")
    if tokens:
        say(f"  MCP tool-surface footprint: ~{tokens['total']} tokens over {tokens['attempts']} "
            f"attempts (~{tokens['avg_per_attempt']}/attempt).")


def _write_transcript(path: Path, say: Trace, results, tokens) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    outcome = " → ".join((r.chosen_modcod or "none").upper() for r in results)
    head = [
        "# gr-autopilot — autonomous mission transcript (reference policy over the MCP tool surface)",
        "",
        "One agent adapts to a timeline of **hidden** conditions (SNR shifts, a jammer appears then",
        "follows, the threat clears), driven only by framework-graded measurements over MCP — never",
        "told the condition. The three stages (AMC, frequency avoidance, hopping) in one run.",
        "",
        f"- outcome per epoch: **{outcome}**",
        f"- {tokens['attempts']} attempts, ~{tokens['total']} tool-surface tokens "
        f"(~{tokens['avg_per_attempt']}/attempt)",
        "",
        "> The reference policy is a deterministic stand-in for the LLM, calling the exact tools an",
        "> LLM client would. For a real LLM-driven run, point a client at "
        "`python -m gr_autopilot.mcp_server` with the `link_adaptive` goal.",
        "",
        "```",
    ]
    path.write_text("\n".join(head + say.lines + ["```", ""]), encoding="utf-8")


def _serve_and_drive(args, svc, srv, cli, be, workdir: Path) -> int:
    """Serve the dashboard and drive the mission on a loop, writing a telemetry snapshot after every
    link trial so the token counter and modcod/frequency update live as the agent adapts."""
    writer = TelemetryWriter(workdir / "telemetry.json")
    goal = "hold BER ≤ {:g}, maximize efficiency as conditions change — over the MCP tool surface".format(
        args.target_ber)

    def emit(server):
        r, m = svc._last_result, svc._last_metrics
        if r is None or m is None:
            return
        mod = svc._spec.modulation if svc._spec else "?"
        hopping = getattr(be, "hop_plan", None) is not None
        detail = ("hopping " if hopping else "run ") + f"{mod} via MCP"
        try:
            diagnosis = svc.diagnose_signal()          # the agent's vision read of this constellation
        except Exception:
            diagnosis = None
        writer.write(result=r, metrics=m, target_ber=args.target_ber,
                     structure={"modcod": mod, "center_freq_hz": be.center_freq_hz},
                     active_loop={"loop": "outer", "detail": detail},
                     goal=goal, tokens=server.token_stats(), diagnosis=diagnosis)
        time.sleep(max(0.0, args.pause))

    srv.on_run_flowgraph = emit
    serve(str(workdir / "session.jsonl"), str(workdir / "telemetry.json"),
          port=args.serve, background=True)
    print(f"telemetry dashboard live:  http://127.0.0.1:{args.serve}")
    print("driving the autonomous mission on a loop (Ctrl-C to stop)\n")
    quiet = Trace(quiet=True)
    timeline = mission_timeline(_candidates(args))
    agent = _make_agent(cli, args, quiet)
    agent.setup()
    n = 0
    while True:
        n += 1
        results = drive_mission(agent, svc, timeline, quiet)
        outcome = "→".join((r.chosen_modcod or "none").upper() for r in results)
        ts = srv.token_stats()
        print(f"  mission {n}: {outcome}   ({ts['attempts']} attempts, ~{ts['total']} tokens)",
              flush=True)


def _candidates(args):
    return [float(m) * 1e6 for m in args.candidates.split(",")]


def _make_agent(cli, args, say):
    return AutonomousAgent(cli, _candidates(args), target_ber=args.target_ber,
                           hop_rates=[float(r) for r in args.hop_rates.split(",")],
                           confirm_bits=args.confirm_bits, say=say)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", default="2400,2401,2402,2403", help="hop/avoid channel set, MHz")
    ap.add_argument("--hop-rates", default="50,100,200,400", help="hop rates to escalate, Hz")
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--confirm-bits", type=int, default=60_000)
    ap.add_argument("--transcript", default="", help="write a markdown transcript here")
    ap.add_argument("--json-log", default="", help="write the raw JSON-RPC transcript here")
    ap.add_argument("--serve", type=int, default=0, metavar="PORT",
                    help="serve the dashboard on PORT and drive the mission on a loop")
    ap.add_argument("--pause", type=float, default=0.8, help="seconds between trials when serving")
    args = ap.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="gr_autopilot_mission_"))
    be = InterferenceSimBackend(clean_es_n0_db=20.0, center_freq_hz=_candidates(args)[0])
    svc = AutopilotService(backend=be, channel={"clean_es_n0_db": 20.0},
                           ledger_path=workdir / "session.jsonl")
    srv = StdioMCPServer(svc, log_path=args.json_log or None)
    cli = InProcessClient(srv)

    if args.serve:
        return _serve_and_drive(args, svc, srv, cli, be, workdir)

    say = Trace()
    say(f"gr-autopilot — autonomous mission over the MCP tool surface "
        f"({len(cli.list_tools())} tools, protocol {cli.protocol_version})")
    say("one agent, changing hidden conditions; it is never told the SNR or the jammer — it "
        "measures and adapts\n")
    agent = _make_agent(cli, args, say)
    agent.setup()
    results = drive_mission(agent, svc, mission_timeline(_candidates(args)), say)
    tokens = srv.token_stats()
    _summary(results, say, tokens)

    if args.transcript:
        _write_transcript(Path(args.transcript), say, results, tokens)
        say(f"\ntranscript: {args.transcript}")
    if args.json_log:
        say(f"raw JSON-RPC log: {args.json_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
