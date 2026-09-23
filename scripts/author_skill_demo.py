#!/usr/bin/env python3
"""C5 self-improvement: the agent authors a new skill on the grader and promotes it (test-gated).

At a marginal SNR the base rungs leave an efficiency gap (uncoded QPSK fails; BPSK wastes rate). The
agent EXPERIMENTS on the framework grader, discovers a coded rung fills the gap, TEST-GATES it
(re-validate on a fresh trial + an efficiency margin), and PROMOTES it into its own vocabulary — which
then grows N→N+1 and the new skill is immediately usable in a build. Self-improvement as vocabulary
growth, without the agent ever grading its own promotion. All over the MCP tool surface.

Run: python scripts/author_skill_demo.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer


def main() -> int:
    wd = Path(tempfile.mkdtemp(prefix="gr_autopilot_skill_"))
    be = InterferenceSimBackend(clean_es_n0_db=7.0)          # marginal: a gap the base rungs can't fill
    svc = AutopilotService(backend=be, channel={"clean_es_n0_db": 7.0}, ledger_path=wd / "s.jsonl")
    cli = InProcessClient(StdioMCPServer(svc))

    n0 = len(cli.call("list_skills"))
    print(f"vocabulary: {n0} skills (curated primitives; the agent is told neither the SNR nor coding gains)")

    print("\n[author] experiment on the grader at the current (hidden) channel:")
    ladder = ["bpsk", "qpsk", "16qam", "qpsk:conv_k3_r34", "16qam:conv_k3_r34"]
    a = cli.call("author_skill", name="coded_qpsk_r34", ladder=ladder)
    if not a["authored"]:
        print("  nothing to author here:", a["reason"])
        return 0
    b = a["benchmark"]
    print(f"  DISCOVERED {b['modcod']} = {b['efficiency']} bits/sym (measured BER {b['ber']:.1e}) — vs the "
          f"best base rung {b['baseline']} = {b['baseline_efficiency']} bits/sym → +{b['gain']} bits/sym")

    print("\n[promote] test-gate (re-validate on a fresh grader trial + beat the baseline by a margin):")
    p = cli.call("promote_skill", name="coded_qpsk_r34")
    print(f"  {p['reason']}")
    n1 = len(cli.call("list_skills"))
    print(f"  vocabulary grew {n0} → {n1} skills; learned: {p['vocabulary']}")

    print("\n[use] build with the freshly-authored skill and run it:")
    cli.call("build_flowgraph", spec=a["spec"])
    cli.call("run_flowgraph", n_bits=40000)
    m = cli.call("get_metrics")
    print(f"  {a['spec']['structure_id']}: BER={m['BER']:.2e} "
          f"[{'meets' if m['BER'] <= 1e-2 else 'FAILS'} target 1e-2] — the agent now composes with a "
          f"skill it authored itself.")

    print("\n=> the agent grew its own vocabulary by discovering a building block on the grader and "
          "test-gating it — self-improvement, no self-scoring.")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
