"""Power adaptation — the fifth countermeasure (no radios).

The agent owns a TX-power knob (not the path loss): back off for efficiency, boost to hold a rung,
boost to overcome a weak jammer — bounded by a finite budget beyond which it must avoid/hop.
"""
from __future__ import annotations

import json

from gr_autopilot.control.power import PowerController
from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.scoring.metrics import compute_metrics
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer

LADDER = ["bpsk", "qpsk", "16qam"]


def _ber(be, power_db, modcod="16qam"):
    be.set_condition(tx_power_db=power_db)
    r = be.run_link(LinkParams(modulation=modcod, n_payload_bits=40_000, sps=8, seed=0))
    return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms).ber


# -- backend knob ---------------------------------------------------------------------------------

def test_tx_power_raises_sinr_and_lowers_ber():
    be = InterferenceSimBackend(clean_es_n0_db=9.0)          # marginal for 16-QAM
    assert _ber(be, +8.0) < _ber(be, 0.0)                    # boosting power improves the link


# -- min-power search -----------------------------------------------------------------------------

def test_min_power_backs_off_at_high_snr():
    r = PowerController(InterferenceSimBackend(clean_es_n0_db=25.0), bits=40_000).min_power("16qam")
    assert r.feasible and r.min_power_db < 0                 # can hold 16-QAM below full power


def test_min_power_boosts_within_budget_at_marginal_snr():
    r = PowerController(InterferenceSimBackend(clean_es_n0_db=10.0), bits=40_000).min_power("16qam")
    assert r.feasible and r.min_power_db > 0                 # needs a boost, but within the budget


def test_min_power_infeasible_beyond_the_budget():
    be = InterferenceSimBackend(clean_es_n0_db=18.0, interferer=Interferer(2.4e9, inr_db=30.0))
    r = PowerController(be, bits=40_000).min_power("16qam")
    assert not r.feasible and r.min_power_db is None         # even max power fails -> not feasible


# -- adapt (joint power + rate) -------------------------------------------------------------------

def test_adapt_holds_the_highest_feasible_rung():
    r = PowerController(InterferenceSimBackend(clean_es_n0_db=10.0), bits=40_000).adapt(LADDER)
    assert r is not None and r.modcod == "16qam" and r.efficiency == 4.0   # power buys the top rung


def test_adapt_returns_none_over_budget_so_the_agent_avoids_or_hops():
    be = InterferenceSimBackend(clean_es_n0_db=18.0, interferer=Interferer(2.4e9, inr_db=30.0))
    assert PowerController(be, bits=40_000).adapt(LADDER) is None


# -- over the MCP tool surface --------------------------------------------------------------------

def test_set_tx_power_over_mcp_clamps_and_helps():
    be = InterferenceSimBackend(clean_es_n0_db=9.0)
    svc = AutopilotService(backend=be, channel={"clean_es_n0_db": 9.0})
    cli = InProcessClient(StdioMCPServer(svc))
    assert "set_tx_power" in {t["name"] for t in cli.list_tools()}
    out = cli.call("set_tx_power", power_db=100.0)           # beyond the PA ceiling
    assert out["clamped"] and out["tx_power_db"] == 6.0      # clamped to the budget
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("16qam").to_dict())
    cli.call("run_flowgraph", n_bits=40_000)
    boosted = cli.call("get_metrics")["BER"]
    cli.call("set_tx_power", power_db=0.0)
    cli.call("run_flowgraph", n_bits=40_000)
    assert boosted < cli.call("get_metrics")["BER"]          # the boost helped
    assert "clean_es_n0" not in json.dumps(out)              # no hidden-channel leak


def test_set_tx_power_needs_a_channel_owning_backend():
    cli = InProcessClient(StdioMCPServer(AutopilotService(channel={"es_n0_db": 12.0})))
    try:
        cli.call("set_tx_power", power_db=3.0)
        assert False, "expected a tool error"
    except Exception as exc:
        assert "power" in str(exc).lower()
