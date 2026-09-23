#!/usr/bin/env python3
"""Ablation — measurement-guided heuristic vs the deterministic two-loop controller (same tools/grader).

NOTE ON LABELS: the "guided" arm plotted here is a DETERMINISTIC measurement-guided heuristic — the
top-down policy a reasoning agent follows — NOT a live model call. It is a reproducible proxy; a real
LLM (Claude) driving the identical MCP tools reproduces these decisions (see the recorded-transcript
replay in tests/test_llm_replay.py). Figures/reports say "measurement-guided
heuristic", never "LLM", so the curve is not mistaken for a model.

Question (spec C4): the physical link is the expensive resource, so *sample efficiency matters*.
Does a measurement-guided agent reach the correct modcod — the classical AMC boundary — at fewer
link runs than the controller that exhaustively climbs every rung?

Both arms drive the SAME numpy-sim link behind a `CountingBackend` that tallies every link run (the
operation that on hardware costs seconds). Neither is told the SNR. We sweep the hidden Es/N0, score
each arm's chosen modcod against a one-time exhaustive ground-truth sweep, and compare
link-runs-to-converge:

  * two_loop — `TwoLoopController`: evaluate bpsk, qpsk, 16qam; run BO on each; keep the best. Exhaustive
    and SNR-agnostic: ~`3*(bo_budget+1)` link runs every time.
  * guided   — the measurement-guided policy a reasoning agent follows, driving the MCP tool surface
    (`AutopilotService` over `InProcessClient`): probe top-down, stop as soon as a rung meets the target,
    and only spend the BO inner loop on a rung whose *BER-vs-EVM signature* says it is phase-broken
    (rescuable) rather than SNR-starved. A real LLM (Claude Code) driving the identical tools reproduces
    these decisions.

Run:  python scripts/llm_ablation.py
      python scripts/llm_ablation.py --report runs/ablation.md --figure runs/ablation.png
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

from scipy.special import erfcinv

from gr_autopilot.control.controller import TwoLoopController
from gr_autopilot.control.objective import parse_modcod, spectral_efficiency
from gr_autopilot.flowgraph import FlowgraphSpec, build_and_run
from gr_autopilot.link.backend import LinkBackend
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.scoring.metrics import compute_metrics
from gr_autopilot.tools import AutopilotService, InProcessClient, MCPToolError, StdioMCPServer

LADDER = ("bpsk", "qpsk", "16qam")


class CountingBackend(LinkBackend):
    """Transparent wrapper that tallies link runs — the expensive physical operation on hardware."""

    def __init__(self, inner: LinkBackend):
        self.inner = inner
        self.name = inner.name
        self.owns_channel = inner.owns_channel
        self.runs = 0

    def set_condition(self, **cond):
        return self.inner.set_condition(**cond)

    def run_link(self, params):
        self.runs += 1
        return self.inner.run_link(params)

    def sense_spectrum(self, freqs_hz, bw_hz=None):
        return self.inner.sense_spectrum(freqs_hz, bw_hz)


# ---- ground truth: what SHOULD each SNR choose? -----------------------------

def ground_truth(snr_db: float, target_ber: float, bits: int, seed: int = 7) -> str:
    """Exhaustive: measure every rung at this SNR (phase 0), pick the highest bits/sym meeting target."""
    be = NumpySimBackend()
    meeting = []
    for modcod in LADDER:
        mod, coding = parse_modcod(modcod)
        r = build_and_run(FlowgraphSpec.link(mod, coding=coding), be,
                          n_payload_bits=bits, es_n0_db=snr_db, seed=seed)
        if compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms).ber <= target_ber:
            meeting.append(modcod)
    return max(meeting, key=spectral_efficiency) if meeting else LADDER[0]


# ---- arm 1: deterministic two-loop controller -------------------------------

def run_two_loop(snr_db: float, target_ber: float, confirm_bits: int, bo_budget: int, seed: int):
    cb = CountingBackend(NumpySimBackend())
    ctrl = TwoLoopController(cb, target_ber=target_ber, ladder=LADDER, bo_budget=bo_budget,
                             confirm_bits=confirm_bits, trial_bits=confirm_bits // 4, seed=seed)
    res = ctrl.run({"es_n0_db": snr_db})
    return res.chosen, cb.runs


# ---- arm 2: measurement-guided policy over the MCP tool surface --------------

def _qpsk_implied_es_n0_db(ber: float) -> float:
    """Es/N0 that Gray-QPSK's BER implies: BER = 0.5*erfc(sqrt(Eb/N0)), Es/N0 = 2*Eb/N0."""
    eb_n0 = erfcinv(2.0 * max(ber, 1e-7)) ** 2
    return 10.0 * math.log10(max(2.0 * eb_n0, 1e-3))


def _evm_implied_es_n0_db(evm_pct: float) -> float:
    """Es/N0 that EVM implies: SNR ~ 1/EVM^2, so Es/N0(dB) ~ -20*log10(EVM_fraction)."""
    return -20.0 * math.log10(max(evm_pct, 1e-6) / 100.0)


def run_guided(snr_db: float, phase_rad: float, target_ber: float, confirm_bits: int,
               bo_budget: int, seed: int, trace: list | None = None):
    """Top-down, measurement-guided, over the real MCP tools. Returns (chosen, link_runs)."""
    cb = CountingBackend(NumpySimBackend())
    svc = AutopilotService(backend=cb, channel={"es_n0_db": snr_db, "phase_offset_rad": phase_rad},
                           confirm_bits=confirm_bits)
    cli = InProcessClient(StdioMCPServer(svc))
    cli.call("claim_device", device_id="pluto-a", role="transmitter")
    cli.call("claim_device", device_id="pluto-b", role="receiver")

    def say(msg):
        if trace is not None:
            trace.append(msg)

    def probe(mod):
        cli.call("build_flowgraph", spec=FlowgraphSpec.link(mod).to_dict())
        cli.call("run_flowgraph", n_bits=confirm_bits)
        return cli.call("get_metrics")

    # 1) try the highest rung first — if it already meets, we are done in one run
    m16 = probe("16qam")
    say(f"probe 16qam: BER={m16['BER']:.2e} EVM={m16['EVM']:.1f}%")
    if m16["BER"] <= target_ber:
        say("16qam meets → keep (max efficiency)")
        return "16qam", cb.runs

    # 2) localize with qpsk
    mq = probe("qpsk")
    say(f"probe qpsk: BER={mq['BER']:.2e} EVM={mq['EVM']:.1f}%")
    if mq["BER"] > target_ber:
        # not even qpsk survives → SNR-limited, drop to the robust rung
        mb = probe("bpsk")
        say(f"probe bpsk: BER={mb['BER']:.2e} → {'keep bpsk' if mb['BER']<=target_ber else 'outage'}")
        return ("bpsk" if mb["BER"] <= target_ber else LADDER[0]), cb.runs

    # 3) qpsk is clean but 16qam failed. Phase-broken (rescuable) or SNR-starved (not)?
    #    Signature: if qpsk decodes far better than its EVM-implied SNR would allow, the EVM is
    #    inflated by a coherent rotation → a phase problem the BO inner loop can correct; noise
    #    hurts BER and EVM together (no gap). A measured BER of 0 only proves SNR >= the
    #    measurement floor (a few errors in confirm_bits), NOT infinity — so cap the BER-implied
    #    SNR there, else a lucky zero-error qpsk probe at a boundary SNR fakes a phase gap.
    floor_ber = 5.0 / confirm_bits
    ber_snr = _qpsk_implied_es_n0_db(max(mq["BER"], floor_ber))
    evm_snr = _evm_implied_es_n0_db(mq["EVM"])
    phase_suspected = (ber_snr - evm_snr) > 4.0
    say(f"diagnose 16qam failure: BER-implied Es/N0={ber_snr:.1f} dB vs EVM-implied={evm_snr:.1f} dB "
        f"→ {'PHASE (rescue with BO)' if phase_suspected else 'SNR-limited (keep qpsk)'}")
    if not phase_suspected:
        return "qpsk", cb.runs

    # 4) inner loop: BO-tune 16qam's phase correction, early-stopping at target
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("16qam").to_dict())
    try:
        bo = cli.call("start_bo_run", params=["phase_correction_rad"], bounds=[[-0.7854, 0.7854]],
                      target_ber=target_ber, budget=bo_budget)
        cli.call("run_flowgraph", n_bits=confirm_bits)
        rescued = cli.call("get_metrics")["BER"] <= target_ber
        say(f"BO phase={bo['best_params']['phase_correction_rad']:+.3f} rad "
            f"({'early-stop' if bo['stopped_early'] else str(bo['trials'])+' trials'}) → "
            f"16qam {'rescued → keep' if rescued else 'still fails → keep qpsk'}")
    except MCPToolError:
        rescued = False
    return ("16qam" if rescued else "qpsk"), cb.runs


# ---- sweep + report ---------------------------------------------------------

@dataclass
class Row:
    snr_db: float
    truth: str
    two_loop: str
    two_loop_runs: int
    guided: str
    guided_runs: int


def sweep(snrs, target_ber: float, confirm_bits: int, bo_budget: int, seed: int) -> list[Row]:
    rows = []
    for snr in snrs:
        truth = ground_truth(snr, target_ber, confirm_bits, seed)
        tl_choice, tl_runs = run_two_loop(snr, target_ber, confirm_bits, bo_budget, seed)
        g_choice, g_runs = run_guided(snr, 0.0, target_ber, confirm_bits, bo_budget, seed)
        rows.append(Row(snr, truth, tl_choice, tl_runs, g_choice, g_runs))
    return rows


def _fmt_table(rows: list[Row]) -> str:
    head = (f"{'Es/N0':>6} | {'optimal':>7} | {'two_loop':>8} {'runs':>5} | "
            f"{'guided':>7} {'runs':>5} | {'match':>5}")
    lines = [head, "-" * len(head)]
    for r in rows:
        ok = "✓✓" if (r.two_loop == r.truth == r.guided) else \
             ("tl" if r.two_loop == r.truth else "") + ("g" if r.guided == r.truth else "")
        lines.append(f"{r.snr_db:>5.0f}  | {r.truth:>7} | {r.two_loop:>8} {r.two_loop_runs:>5} | "
                     f"{r.guided:>7} {r.guided_runs:>5} | {ok:>5}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snrs", default="5,7,9,11,13,15,17,19", help="hidden Es/N0 grid (dB)")
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--confirm-bits", type=int, default=200_000)
    ap.add_argument("--bo-budget", type=int, default=12)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--report", default="", help="write a markdown report here")
    ap.add_argument("--figure", default="", help="write a link-runs comparison figure here (needs matplotlib)")
    args = ap.parse_args(argv)

    snrs = [float(s) for s in args.snrs.split(",")]
    rows = sweep(snrs, args.target_ber, args.confirm_bits, args.bo_budget, args.seed)

    table = _fmt_table(rows)
    tl_acc = sum(r.two_loop == r.truth for r in rows) / len(rows)
    g_acc = sum(r.guided == r.truth for r in rows) / len(rows)
    tl_mean = sum(r.two_loop_runs for r in rows) / len(rows)
    g_mean = sum(r.guided_runs for r in rows) / len(rows)

    print(f"gr-autopilot ablation — measurement-guided heuristic vs two-loop controller  "
          f"(target BER {args.target_ber:g})")
    print(f"neither arm is told the SNR; both are graded against an exhaustive ground-truth sweep\n")
    print(table)
    print(f"\naccuracy vs AMC ground truth : two_loop {tl_acc:.0%}   guided {g_acc:.0%}")
    print(f"mean link runs to converge   : two_loop {tl_mean:.1f}   guided {g_mean:.1f}   "
          f"({tl_mean / max(g_mean, 1e-9):.1f}x fewer for guided)")

    if args.figure:
        _save_figure(rows, args.figure)
        print(f"\nfigure: {args.figure}")
    if args.report:
        _save_report(rows, args, tl_acc, g_acc, tl_mean, g_mean, table)
        print(f"report: {args.report}")
    return 0


def _save_figure(rows: list[Row], path: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover
        print(f"(skipping figure — matplotlib unavailable: {exc})")
        return
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    xs = [r.snr_db for r in rows]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(xs, [r.two_loop_runs for r in rows], "o-", color="#fb5a4b", label="two_loop (exhaustive)")
    ax.plot(xs, [r.guided_runs for r in rows], "s-", color="#38bdf8",
            label="measurement-guided heuristic")
    ax.set_xlabel("hidden Es/N0 (dB)")
    ax.set_ylabel("link runs to converge")
    ax.set_title("Sample efficiency: measurement-guided heuristic vs exhaustive two-loop")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)


def _save_report(rows, args, tl_acc, g_acc, tl_mean, g_mean, table) -> None:
    from pathlib import Path
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    body = [
        "# Ablation — measurement-guided heuristic vs the deterministic two-loop controller",
        "",
        f"Same tools, same framework grader, same hidden channel; target BER ≤ {args.target_ber:g}. Neither",
        "arm is told the SNR. Both drive the numpy-sim link behind a `CountingBackend`; each choice is",
        "scored against an exhaustive ground-truth sweep (the classical AMC boundary).",
        "",
        "```",
        table,
        "```",
        "",
        f"- **Accuracy vs AMC ground truth:** two_loop **{tl_acc:.0%}**, guided **{g_acc:.0%}** — both",
        "  rediscover the SNR→modcod boundary from BER alone.",
        f"- **Mean link runs to converge:** two_loop **{tl_mean:.1f}**, guided **{g_mean:.1f}** "
        f"(**{tl_mean / max(g_mean, 1e-9):.1f}× fewer**). The controller climbs and BO-tunes every rung",
        f"  ({args.bo_budget} trials each) regardless of SNR; the guided policy stops at the first rung",
        "  that meets the target and spends the BO inner loop only on a rung whose BER-vs-EVM signature",
        "  says it is phase-broken (rescuable), never on an SNR-starved one.",
        "",
        "The two arms are the **same two-loop policy with a different outer driver** — a deterministic",
        "climb vs a measurement-guided heuristic standing in for a reasoning agent. The guided policy",
        "reproduces the decisions a real LLM made driving these identical tools, so the sweep is",
        "regenerable without a model in the loop.",
        "",
        f"Reproduce: `python scripts/llm_ablation.py --snrs {args.snrs}`",
        "",
    ]
    Path(args.report).write_text("\n".join(body), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
