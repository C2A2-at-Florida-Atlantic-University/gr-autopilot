"""The arms race: a co-evolving jammer vs the adaptive agent (Stage 3+, no radios).

Checks the jammer's escalation logic and the two fixed points: a beatable reaction floor is out-hopped
(agent wins), a low-enough floor out-reacts the agent's fastest hop (jammer wins — the 1/tau limit
reached by escalation).
"""
from __future__ import annotations

from gr_autopilot.control.arms_race import AdaptiveJammer, run_arms_race
from gr_autopilot.control.mission import AutonomousAgent
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer

CANDS = [2.400e9 + k * 1e6 for k in range(4)]
RATES = [50.0, 100.0, 200.0, 400.0, 800.0]


def _race(floor_ms):
    be = InterferenceSimBackend(clean_es_n0_db=20.0, center_freq_hz=CANDS[0])
    svc = AutopilotService(backend=be, channel={"clean_es_n0_db": 20.0})
    cli = InProcessClient(StdioMCPServer(svc))
    agent = AutonomousAgent(cli, CANDS, target_ber=1e-2, hop_rates=RATES, confirm_bits=12000)
    agent.setup()
    jam = AdaptiveJammer(start_freq_hz=CANDS[0], inr_db=22.0,
                         reaction_start_s=20e-3, reaction_floor_s=floor_ms * 1e-3)
    return run_arms_race(agent, svc, jam, clean_es_n0_db=20.0)


# -- AdaptiveJammer escalation logic --------------------------------------------------------------

def test_fixed_becomes_a_follower_after_avoidance():
    j = AdaptiveJammer(start_freq_hz=CANDS[0])
    assert j.kind == "fixed" and j.to_interferer(CANDS[2]).reactive is False
    desc = j.escalate("frequency avoidance → AMC", None)
    assert j.kind == "follower" and j.reaction_s == j.reaction_start_s and desc
    itf = j.to_interferer(CANDS[2])
    assert itf.reactive is True and itf.reaction_s == j.reaction_s   # follows the agent, reactively


def test_follower_shrinks_reaction_to_catch_a_hop_rate():
    j = AdaptiveJammer(start_freq_hz=CANDS[0], reaction_start_s=20e-3, reaction_floor_s=1e-3)
    j.escalate("avoidance", None)
    before = j.reaction_s
    j.escalate("hopping @ 100 Hz", 100.0)
    assert j.reaction_s < before and j.reaction_s <= 1.0 / 100.0    # now fast enough to catch 100 Hz


def test_follower_concedes_when_it_cannot_get_faster():
    j = AdaptiveJammer(start_freq_hz=CANDS[0], reaction_start_s=5e-3, reaction_floor_s=5e-3)
    j.escalate("avoidance", None)                                   # follower already at the floor
    assert j.escalate("hopping @ 200 Hz", 200.0) is None           # cannot catch 200 Hz -> concede


# -- the two fixed points -------------------------------------------------------------------------

def test_agent_wins_a_beatable_jammer():
    res = _race(5.0)                                                # floor 200 Hz; agent max 800 Hz
    assert res.winner == "agent"
    assert res.final_hop_rate_hz and res.final_hop_rate_hz >= 200.0
    assert res.rounds >= 3                                          # a real escalation, not a one-shot
    assert "avoid" in res.ladder[0]["agent_action"].lower()        # it starts by avoiding the fixed jammer


def test_jammer_wins_a_fast_follower():
    res = _race(0.4)                                               # floor 2500 Hz > agent's fastest hop
    assert res.winner == "jammer"
    assert res.ladder[-1]["agent_won"] is False


def test_escalation_is_monotonic_the_jammer_only_speeds_up():
    res = _race(0.4)
    hops = [r["hop_rate_hz"] for r in res.ladder if r["hop_rate_hz"]]
    assert hops == sorted(hops)                                    # the agent's hop rate never decreases
