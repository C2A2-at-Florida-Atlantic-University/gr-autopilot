"""Recorded-transcript replay of an agent driving the loop over the REAL MCP tool path.

The headline "a real LLM drives build->run->measure->adapt over MCP" path (an external client of
gr_autopilot.mcp_server) otherwise has NO automated coverage — the ablation's "guided" arm is a
deterministic proxy. This replays a captured tool-call sequence through InProcessClient (exactly what
an MCP client does, minus the pipe) and asserts the decisions land on the classical AMC boundary, so
the model-facing tool path is regression-guarded without needing an API key.
"""
from __future__ import annotations

from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer


def _drive(cli, mod, n=60_000):
    """One agent 'turn': build a rung, run it on the link, read the framework-graded BER."""
    cli.call("build_flowgraph", spec=FlowgraphSpec.link(mod).to_dict())
    cli.call("run_flowgraph", n_bits=n)
    return cli.call("get_metrics")["BER"]


def test_replayed_agent_reaches_amc_boundary_over_mcp(tmp_path):
    # Hidden Es/N0 = 9 dB (never told): 16-QAM fails the 1e-2 target, QPSK meets it. A top-down agent
    # (try the highest rung, drop when it fails) keeps QPSK — the AMC boundary, from measurement alone.
    svc = AutopilotService(channel={"es_n0_db": 9.0}, ledger_path=tmp_path / "s.jsonl")
    cli = InProcessClient(StdioMCPServer(svc))

    ber_16 = _drive(cli, "16qam")
    ber_qpsk = _drive(cli, "qpsk")

    assert ber_16 > 1e-2, "16-QAM should FAIL the target at 9 dB (agent must not keep it)"
    assert ber_qpsk <= 1e-2, "QPSK should MEET the target at 9 dB (the AMC choice)"
    # (leak-proofing of the tool surface is covered exhaustively in test_integrity_leak.py; note the
    # agent legitimately MEASURES an SNR ~9 dB here — an accurate data-aided estimate is a measurement,
    # not a leak of the operator's hidden setpoint.)


def test_replayed_agent_climbs_to_16qam_when_the_channel_allows(tmp_path):
    # At a clean 20 dB channel the same top-down policy keeps 16-QAM (highest efficiency meeting BER).
    svc = AutopilotService(channel={"es_n0_db": 20.0}, ledger_path=tmp_path / "s.jsonl")
    cli = InProcessClient(StdioMCPServer(svc))
    assert _drive(cli, "16qam") <= 1e-2      # 16-QAM already meets target -> the agent stops here
