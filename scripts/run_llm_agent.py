#!/usr/bin/env python3
"""Drive the gr-autopilot loop through the MCP tool surface, and capture the transcript.

There are two ways to drive the SAME tools (``gr_autopilot.tools.tool_manifest`` bound to
``AutopilotService``), and this script is about making that seam concrete:

  * an ACTUAL LLM -- point an MCP client at the stdio server and ask it to optimize the link:
        claude mcp add gr-autopilot -- python -m gr_autopilot.mcp_server
    The model then chooses every tool call itself. That is the real "LLM in the loop", and it is
    what unblocks the pure-LLM-vs-two-loop ablation (same tools, LLM outer loop instead of the
    deterministic TwoLoopController).

  * this script's --reference policy -- a small DETERMINISTIC driver that exercises the surface
    end-to-end: discover the experiment, claim the radios, then climb the modulation ladder,
    rescuing a failed rung with the Bayesian-optimization inner loop before dropping back. It
    stands in for the LLM to smoke-test that the tools are *sufficient* to run the whole loop, and
    to emit a transcript in the same shape. It is NOT an LLM.

Either way the driver is never told the channel SNR (it is held privately in the service); it
climbs from framework-graded BER alone. The channel here carries a phase offset so the aggressive
16-QAM rung fails until the inner loop tunes it -- the two loops visibly cooperating.

With ``--serve PORT`` the driver serves the read-only telemetry dashboard and drives the loop in a
loop, writing a snapshot after each attempt that carries the MCP server's token_stats() -- so the
dashboard header shows the **tool-surface token footprint** growing per attempt (a proxy for the
driving LLM's token cost; the server can't see the client's true tokenizer).

Run:  python scripts/run_llm_agent.py --reference
      python scripts/run_llm_agent.py --serve 8080   # + watch the token counter on the dashboard
      python scripts/run_llm_agent.py --reference --es-n0-db 18 --phase-offset 0.5 --target-ber 1e-2 \
             --transcript runs/agent_transcript.md --json-log runs/agent_rpc.jsonl
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.telemetry import TelemetryWriter
from gr_autopilot.telemetry.server import serve
from gr_autopilot.tools import AutopilotService, InProcessClient, MCPToolError, StdioMCPServer

BITS_PER_SYM = {"bpsk": 1, "qpsk": 2, "16qam": 4}
LADDER = ("bpsk", "qpsk", "16qam")


class Trace:
    """Collects human-readable narration for the markdown transcript while echoing to stdout."""

    def __init__(self, quiet: bool = False):
        self.lines: list[str] = []
        self.quiet = quiet

    def __call__(self, line: str = "") -> None:
        self.lines.append(line)
        if not self.quiet:
            print(line, flush=True)


def reference_drive(cli: InProcessClient, target_ber: float, confirm_bits: int, say: Trace) -> dict:
    """A deterministic stand-in for the LLM: climb the ladder, tune a failed rung with BO, keep the
    highest-efficiency structure that meets the BER target."""
    say("[discovery]")
    exp = cli.call("describe_experiment", name="link_only")
    say(f"  goal: {exp['success']}")
    say(f"  rf path: {cli.call('get_rf_path_config')['path']}")
    skills = [s["name"] for s in cli.call("list_skills")]
    say(f"  {len(skills)} skills available: {', '.join(skills[:6])}, …")
    devs = cli.call("list_devices")
    tx, rx = devs[0]["device_id"], devs[1]["device_id"]
    say(f"  claim {tx}=transmitter, {rx}=receiver")
    for dev, role in ((tx, "transmitter"), (rx, "receiver")):
        try:
            cli.call("claim_device", device_id=dev, role=role)
        except MCPToolError:
            pass  # already claimed on a re-drive (the serve loop); fine
    try:
        cli.call("claim_device", device_id=rx, role="grader")
        say("  (claimed the grader?! integrity broken)")
    except MCPToolError:
        say(f"  grader on {rx} is framework-reserved — cannot be claimed (integrity split holds)")

    say("")
    say("[outer loop: climb the modulation ladder — highest efficiency that meets BER]")
    best = None
    for mod in LADDER:
        spec = FlowgraphSpec.link(mod).to_dict()
        cli.call("build_flowgraph", spec=spec)
        cli.call("run_flowgraph", n_bits=confirm_bits)
        m = cli.call("get_metrics")
        tag = "meets" if m["BER"] <= target_ber else "FAILS"
        say(f"  {mod:>6} ({BITS_PER_SYM[mod]} b/sym): BER={m['BER']:.2e} EVM={m['EVM']:.1f}%  [{tag} target {target_ber:g}]")

        if m["BER"] > target_ber:
            # inner loop: try to rescue this rung by tuning the carrier phase before giving up
            say(f"         inner loop → start_bo_run(phase_correction_rad) to rescue {mod}")
            bo = cli.call("start_bo_run", params=["phase_correction_rad"],
                          bounds=[[-0.7854, 0.7854]], target_ber=target_ber, budget=16)
            cli.call("run_flowgraph", n_bits=confirm_bits)
            m = cli.call("get_metrics")
            phase = bo["best_params"].get("phase_correction_rad", 0.0)
            tag = "meets" if m["BER"] <= target_ber else "FAILS"
            note = "stopped early — target reached" if bo["stopped_early"] else f"{bo['trials']} trials"
            say(f"         BO best phase={phase:+.3f} rad ({note}) → re-run BER={m['BER']:.2e}  [{tag}]")

        if m["BER"] <= target_ber:
            best = {"modulation": mod, "bits_per_sym": BITS_PER_SYM[mod], "ber": m["BER"],
                    "evm_pct": m["EVM"], "snr_db": m["SNR"]}
        else:
            say(f"         {mod} fails even after tuning → stop; keep the last rung that met target")
            break

    say("")
    if best:
        say(f"=> KEEP {best['modulation'].upper()} — {best['bits_per_sym']} bits/sym, "
            f"BER={best['ber']:.2e} (highest spectral efficiency meeting BER ≤ {target_ber:g})")
    else:
        say("=> no modulation met the target on this channel")
    return best or {}


def _write_transcript(path: Path, say: Trace, args, best: dict, n_rpc: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    head = [
        "# gr-autopilot — agent transcript (reference policy over the MCP tool surface)",
        "",
        f"- backend: sim (hidden Es/N0 = {args.es_n0_db} dB, phase offset = {args.phase_offset} rad — "
        "**withheld from the agent**)",
        f"- goal: BER ≤ {args.target_ber:g}, maximize spectral efficiency",
        f"- outcome: **{best.get('modulation', 'none').upper()}** "
        f"({best.get('bits_per_sym', 0)} bits/sym) at BER={best.get('ber', float('nan')):.2e}",
        f"- {n_rpc} MCP requests (initialize + tool calls), each with a graded response",
        "",
        "> The reference policy is a deterministic stand-in for the LLM, driving the exact tools an",
        "> LLM client would. For a real LLM-driven run, point a client at "
        "`python -m gr_autopilot.mcp_server`.",
        "",
        "```",
    ]
    path.write_text("\n".join(head + say.lines + ["```", ""]), encoding="utf-8")


def _serve_and_drive(args, service, server, cli, workdir: Path) -> int:
    """Serve the read-only dashboard and drive the reference policy over MCP in a loop, writing a
    telemetry snapshot after every attempt. The snapshot carries the server's token_stats(), so the
    dashboard header shows the MCP tool-surface token footprint growing per attempt."""
    goal = "meet BER ≤ {:g}, maximize spectral efficiency — driven over the MCP tool surface".format(
        args.target_ber)
    writer = TelemetryWriter(workdir / "telemetry.json")

    def emit(srv):  # fired by the server after each successful run_flowgraph
        r, m = service._last_result, service._last_metrics
        if r is None or m is None:
            return
        mod = service._spec.modulation if service._spec else "?"
        writer.write(result=r, metrics=m, target_ber=args.target_ber,
                     structure={"modcod": mod, "center_freq_hz": None},
                     active_loop={"loop": "outer", "detail": f"run {mod} via MCP tool call"},
                     goal=goal, tokens=srv.token_stats())
        time.sleep(max(0.0, args.pause))

    server.on_run_flowgraph = emit
    serve(str(workdir / "session.jsonl"), str(workdir / "telemetry.json"),
          port=args.serve, background=True)
    print(f"telemetry dashboard live:  http://127.0.0.1:{args.serve}")
    print("driving the loop over MCP — the token counter in the header grows per attempt "
          "(Ctrl-C to stop)\n")
    quiet = Trace(quiet=True)
    n = 0
    while True:
        n += 1
        reference_drive(cli, args.target_ber, args.confirm_bits, quiet)
        ts = server.token_stats()
        print(f"  pass {n}: {ts['attempts']} attempts · ~{ts['total']} tokens total · "
              f"{ts['avg_per_attempt']}/attempt", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference", action="store_true",
                    help="run the deterministic reference policy (default if no driver chosen)")
    ap.add_argument("--es-n0-db", type=float, default=18.0, help="hidden sim Es/N0 (withheld)")
    ap.add_argument("--phase-offset", type=float, default=0.5,
                    help="hidden carrier phase offset (rad) — makes 16-QAM need the BO inner loop")
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--confirm-bits", type=int, default=200_000)
    ap.add_argument("--transcript", default="", help="write a markdown transcript here")
    ap.add_argument("--json-log", default="", help="write the raw JSON-RPC transcript here")
    ap.add_argument("--serve", type=int, default=0, metavar="PORT",
                    help="serve the telemetry dashboard on PORT and drive continuously — the MCP "
                         "token counter grows in the dashboard header, per attempt")
    ap.add_argument("--pause", type=float, default=1.2, help="seconds between attempts when serving")
    args = ap.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="gr_autopilot_agent_"))
    service = AutopilotService(
        channel={"es_n0_db": args.es_n0_db, "phase_offset_rad": args.phase_offset},
        ledger_path=workdir / "session.jsonl")
    server = StdioMCPServer(service, log_path=args.json_log or None)
    cli = InProcessClient(server)

    if args.serve:
        return _serve_and_drive(args, service, server, cli, workdir)

    say = Trace()
    say(f"gr-autopilot — agent driving the MCP tool surface   ({len(cli.list_tools())} tools, "
        f"protocol {cli.protocol_version})")
    say("the agent is NOT told the channel SNR; it climbs from measured BER alone")
    say("")
    best = reference_drive(cli, args.target_ber, args.confirm_bits, say)

    n_requests = cli._id  # id'd requests (initialize + every tool call); each gets one response
    if args.transcript:
        _write_transcript(Path(args.transcript), say, args, best, n_requests)
        say("")
        say(f"transcript: {args.transcript}")
    if args.json_log:
        say(f"raw JSON-RPC log: {args.json_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
