"""Autonomous cross-stage mission over the MCP tool surface (Stages 1-3), no radios.

Covers the tool-surface extensions (sense_spectrum / set_center_freq / set_hop_plan) with the
integrity split intact, and the AutonomousAgent adapting across a timeline of hidden conditions
(SNR drop -> AMC down; fixed jammer -> avoid; reactive jammer -> hop; threat clears -> climb back).
"""
from __future__ import annotations

import json

import pytest

from gr_autopilot.control.mission import AutonomousAgent
from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer

CANDS = [2.400e9 + k * 1e6 for k in range(4)]


def _cli(clean=20.0, interferer=None):
    be = InterferenceSimBackend(clean_es_n0_db=clean, interferer=interferer, center_freq_hz=CANDS[0])
    svc = AutopilotService(backend=be, channel={"clean_es_n0_db": clean, "interferer": interferer})
    return svc, be, InProcessClient(StdioMCPServer(svc))


# -- tool-surface extensions -------------------------------------------------------------------

def test_stage23_tools_registered():
    _, _, cli = _cli()
    names = {t["name"] for t in cli.list_tools()}
    assert {"sense_spectrum", "set_center_freq", "set_hop_plan", "clear_hop_plan"} <= names


def test_sense_spectrum_locates_a_fixed_jammer():
    _, _, cli = _cli(interferer=Interferer(CANDS[0], inr_db=25.0, kind="cw"))
    occ = {round(o["center_freq_hz"]): o["occupied"] for o in cli.call("sense_spectrum", freqs_hz=CANDS)}
    assert occ[round(CANDS[0])] is True
    assert all(occ[round(f)] is False for f in CANDS[1:])   # narrowband jammer, one channel


def test_sense_spectrum_reactive_jammer_is_invisible():
    # a reactive jammer stays idle while the link is silent -> energy detection reads clear
    _, _, cli = _cli(interferer=Interferer(CANDS[0], inr_db=25.0, kind="cw", reactive=True, reaction_s=5e-3))
    assert all(not o["occupied"] for o in cli.call("sense_spectrum", freqs_hz=CANDS))


def test_set_center_freq_avoids_a_fixed_jammer():
    _, _, cli = _cli(clean=18.0, interferer=Interferer(CANDS[0], inr_db=25.0, kind="cw"))
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("qpsk").to_dict())
    cli.call("run_flowgraph", n_bits=40000)
    assert cli.call("get_metrics")["BER"] > 1e-2                 # jammed on CANDS[0]
    cli.call("set_center_freq", center_freq_hz=CANDS[2])
    cli.call("run_flowgraph", n_bits=40000)
    assert cli.call("get_metrics")["BER"] <= 1e-2               # restored off the jammer


def test_set_hop_plan_evades_a_reactive_jammer():
    _, be, cli = _cli(clean=18.0,
                      interferer=Interferer(2.4e9, inr_db=20.0, kind="cw", reactive=True, reaction_s=5e-3))
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("qpsk").to_dict())
    cli.call("run_flowgraph", n_bits=40000)
    assert cli.call("get_metrics")["BER"] > 1e-2                 # reactive -> broken on one channel
    out = cli.call("set_hop_plan", channels_hz=CANDS, hop_rate_hz=200.0)  # 200 > 1/tau (=200) -> evade
    assert out["hop_rate_hz"] == 200.0
    cli.call("run_flowgraph", n_bits=40000)
    assert cli.call("get_metrics")["BER"] <= 1e-2               # out-hopped
    assert cli.call("clear_hop_plan")["was_hopping"] is True


def test_hidden_channel_never_leaks_on_the_agent_surface():
    svc, _, cli = _cli(clean=18.0, interferer=Interferer(CANDS[0], inr_db=25.0, kind="cw"))
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("qpsk").to_dict())
    cli.call("run_flowgraph", n_bits=20000)
    blob = json.dumps([cli.call("get_status"), cli.call("get_metrics"),
                       cli.call("describe_experiment", name="link_adaptive"),
                       cli.call("sense_spectrum", freqs_hz=CANDS)])
    for forbidden in ("clean_es_n0_db", "effective_es_n0_db", "es_n0", "inr_db", "interferer_present"):
        assert forbidden not in blob


def test_set_center_freq_requires_a_channel_owning_backend():
    # the default numpy backend does not own an agent-tunable center frequency
    svc = AutopilotService(channel={"es_n0_db": 12.0})
    cli = InProcessClient(StdioMCPServer(svc))
    with pytest.raises(Exception):
        cli.call("set_center_freq", center_freq_hz=2.4e9)


# -- the autonomous agent across a changing timeline -------------------------------------------

def _run_timeline(agent, svc, timeline):
    out = {}
    for label, cond in timeline:
        svc.set_channel(**cond)
        out[label] = agent.adapt(label)
    return out


def test_mission_full_three_stage_arc():
    svc, be, cli = _cli(clean=20.0)
    agent = AutonomousAgent(cli, CANDS, target_ber=1e-2, confirm_bits=40000)
    agent.setup()
    fixed = Interferer(CANDS[0], inr_db=25.0, kind="cw")
    reactive = Interferer(2.4e9, inr_db=20.0, kind="cw", reactive=True, reaction_s=5e-3)
    r = _run_timeline(agent, svc, [
        ("A", {"clean_es_n0_db": 20.0, "interferer": None}),
        ("B", {"clean_es_n0_db": 6.0, "interferer": None}),
        ("C", {"clean_es_n0_db": 18.0, "interferer": fixed}),
        ("D", {"clean_es_n0_db": 18.0, "interferer": reactive}),
        ("E", {"clean_es_n0_db": 20.0, "interferer": None}),
    ])
    assert all(r[k].meets for k in "ABCDE")                       # link held at every epoch
    assert r["A"].chosen_modcod == "16qam"                       # climbs to the top rung
    assert r["B"].bits_per_sym < 4                               # AMC drops on the SNR fall
    assert r["C"].chosen_modcod == "16qam" and r["C"].center_freq_hz != CANDS[0]  # avoided
    assert "avoidance" in r["C"].action
    assert r["D"].hop_rate_hz is not None and "hop" in r["D"].action.lower()      # hopped
    assert r["E"].hop_rate_hz is None                            # dropped hopping, threat gone


def test_mission_avoidance_moves_off_the_jammed_channel():
    svc, be, cli = _cli(clean=18.0)
    agent = AutonomousAgent(cli, CANDS, target_ber=1e-2, confirm_bits=40000)
    agent.setup()
    svc.set_channel(clean_es_n0_db=18.0, interferer=Interferer(CANDS[0], inr_db=25.0, kind="cw"))
    res = agent.adapt("jam")
    assert res.meets and res.center_freq_hz in CANDS[1:]         # landed on a clear channel
    assert be.interferer is not None                            # jammer still present, just avoided
