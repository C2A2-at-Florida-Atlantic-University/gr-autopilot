"""Co-evolving adversary (the arms race) — a jammer that ESCALATES against the agent's countermeasure.

Stages 2-3 pit the agent against a *fixed* adversary. Here the jammer adapts back, so the two
co-evolve in an escalating loop, each move forcing the next:

    fixed jammer on the link's channel
        -> agent AVOIDS (retunes to a clear channel) and restores the link
    jammer becomes a channel-FOLLOWER (reactive: idle while silent, then jams wherever the link went)
        -> agent HOPS faster than the follower can retune (hop rate above 1/tau) and restores
    jammer SHRINKS its reaction latency to catch that hop rate
        -> agent hops FASTER ...
    ... until a fixed point:
      * the jammer hits its reaction FLOOR (hardware limit) while the agent can still out-hop it
        -> the AGENT wins with a stable hopped link, or
      * the jammer can react faster than the agent's fastest achievable hop
        -> the JAMMER wins (the 1/tau limit the hopping demo characterizes, reached by escalation).

Pure physics reuse: ``InterferenceSimBackend`` + ``Interferer`` (reactive, reaction_s) +
``link/hopping.py`` (jammed_fraction = max(0, 1 - tau*hop_rate)). The agent side is the same
``AutonomousAgent`` that drives the MCP tools (AMC -> avoid -> hop); the jammer only reacts to *which*
countermeasure it observes, never to hidden agent state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gr_autopilot.link.interference import Interferer


@dataclass
class AdaptiveJammer:
    """A jammer that escalates: fixed -> channel-follower -> faster follower, toward a reaction floor."""

    start_freq_hz: float
    inr_db: float = 22.0
    reaction_start_s: float = 20e-3      # first follower reaction latency (slow)
    reaction_floor_s: float = 5e-3       # cannot react faster than this (its hardware) — the knob that
                                         # decides the outcome: 1/floor is the fastest hop it can catch
    kind: str = "fixed"                  # "fixed" | "follower"
    reaction_s: float = 0.0
    level: int = 0

    def to_interferer(self, agent_center_freq_hz: float) -> Interferer:
        """The interferer to apply this round, given where the agent currently sits."""
        if self.kind == "fixed":
            return Interferer(center_freq_hz=self.start_freq_hz, inr_db=self.inr_db, kind="cw")
        # follower: reactive, so it jams wherever the link transmits (center is nominal only)
        return Interferer(center_freq_hz=agent_center_freq_hz, inr_db=self.inr_db, kind="cw",
                          reactive=True, reaction_s=self.reaction_s)

    def escalate(self, agent_action: str, hop_rate_hz: float | None) -> str | None:
        """React to the agent's winning countermeasure. Returns a human description of the escalation,
        or ``None`` if the jammer cannot escalate further (it concedes → the agent wins)."""
        if self.kind == "fixed":
            # the agent avoided a fixed jammer -> start following it (reactively)
            self.kind = "follower"
            self.reaction_s = self.reaction_start_s
            self.level = 1
            return (f"agent avoided → jammer becomes a channel-follower, reaction "
                    f"{self.reaction_s * 1e3:.1f} ms (catches hops below {1.0 / self.reaction_s:.0f} Hz)")
        # the agent out-hopped at hop_rate_hz -> shrink reaction to catch that rate (need tau < 1/R)
        if not hop_rate_hz:
            return None
        target = 0.9 / hop_rate_hz
        if target >= self.reaction_s:            # already fast enough for this rate — nothing to do
            target = self.reaction_s * 0.5
        new = max(self.reaction_floor_s, target)
        if new >= self.reaction_s - 1e-9:        # at the floor: cannot get faster -> concede
            return None
        self.reaction_s = new
        self.level += 1
        return (f"agent hopped at {hop_rate_hz:.0f} Hz → jammer speeds reaction to "
                f"{new * 1e3:.2f} ms (now catches hops below {1.0 / new:.0f} Hz)")


@dataclass
class ArmsRaceResult:
    winner: str                          # "agent" | "jammer" | "inconclusive" (no fixed point in max_rounds)
    rounds: int
    ladder: list = field(default_factory=list)
    final_hop_rate_hz: float | None = None
    reaction_floor_hz: float | None = None


def run_arms_race(agent, svc, jammer: AdaptiveJammer, clean_es_n0_db: float = 20.0,
                  max_rounds: int = 10, say=lambda *a: None) -> ArmsRaceResult:
    """Alternate agent-adapt / jammer-escalate until a fixed point. ``agent`` is an AutonomousAgent
    bound to a service on an InterferenceSimBackend; ``svc`` is that AutopilotService (the framework
    sets the hidden interferer each round via set_channel)."""
    ladder = []
    for rnd in range(max_rounds):
        itf = jammer.to_interferer(agent.center_freq)
        svc.set_channel(clean_es_n0_db=clean_es_n0_db, interferer=itf)   # hidden condition this round
        jstate = (f"{jammer.kind}" + (f" τ={jammer.reaction_s*1e3:.2f}ms" if jammer.kind == "follower" else
                                      f"@{jammer.start_freq_hz/1e6:.0f}MHz"))
        res = agent.adapt(f"r{rnd}")
        won = res.meets
        row = {"round": rnd, "jammer": jstate, "agent_action": res.action,
               "hop_rate_hz": res.hop_rate_hz, "modcod": res.chosen_modcod, "agent_won": won}
        say(f"[round {rnd}] jammer: {jstate}")
        say(f"           agent: {res.action} → {(res.chosen_modcod or 'NO LINK').upper()} "
            f"[{'restored' if won else 'BEATEN'}]")

        if not won:                       # the agent could not restore -> the jammer wins
            ladder.append(row)
            say("  => JAMMER WINS: the agent's fastest countermeasure no longer restores the link.\n")
            return ArmsRaceResult("jammer", rnd + 1, ladder,
                                  reaction_floor_hz=1.0 / jammer.reaction_floor_s)
        esc = jammer.escalate(res.action, res.hop_rate_hz)
        row["escalation"] = esc
        ladder.append(row)
        if esc is None:                   # the jammer cannot escalate -> the agent wins
            say(f"  => AGENT WINS: out-hops the jammer's {jammer.reaction_floor_s*1e3:.1f} ms reaction "
                f"floor at {res.hop_rate_hz:.0f} Hz — a stable hopped link.\n")
            return ArmsRaceResult("agent", rnd + 1, ladder, final_hop_rate_hz=res.hop_rate_hz,
                                  reaction_floor_hz=1.0 / jammer.reaction_floor_s)
        say(f"           escalate: {esc}\n")
    # Exhausted max_rounds with neither terminal condition reached: the jammer was still escalating
    # and could yet flip the outcome, so this is NOT an agent win — report it honestly as unresolved.
    say("  => INCONCLUSIVE: no fixed point within max_rounds (the jammer was still escalating).\n")
    return ArmsRaceResult("inconclusive", max_rounds, ladder,
                          reaction_floor_hz=1.0 / jammer.reaction_floor_s)
