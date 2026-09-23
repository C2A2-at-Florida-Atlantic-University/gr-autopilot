"""Autonomous cross-stage mission policy (Stages 1-3) over the MCP tool surface.

ONE policy that adapts to CHANGING hidden conditions from measurement alone — the synthesis of the
three stages into a single sustained, unattended run:

  * Stage 1 (AMC): climb the modulation ladder for efficiency, drop it when the SNR falls, rescuing a
    marginal rung with the Bayesian-optimization inner loop.
  * Stage 2 (avoidance): if the link is broken at every rung, ``sense_spectrum`` for a jammer and
    ``set_center_freq`` to a clear channel.
  * Stage 3 (hopping): if every channel senses clear yet the link is STILL broken, the jammer is
    reactive (it follows) — ``set_hop_plan`` and escalate the hop rate until BER meets.

It drives the SAME MCP tools a real LLM client would call (an ``InProcessClient`` here). It is the
deterministic *reference agent* — not an LLM — but a faithful stand-in proving the tool surface is
sufficient to run the whole loop unattended across changing conditions. Point a real MCP client at
``python -m gr_autopilot.mcp_server`` to swap an actual LLM into the identical seam.
"""
from __future__ import annotations

from dataclasses import dataclass

from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.tools.mcp_stdio import MCPToolError

from gr_autopilot.modulation import MODULATIONS as _MODS, bits_per_symbol as _bps
BITS_PER_SYM = {m: _bps(m) for m in _MODS}
_PHASE_BOUNDS = [[-0.7853981633974483, 0.7853981633974483]]  # +-pi/4, the BO phase-correction knob


@dataclass
class EpochResult:
    """What the agent did in one epoch (one hidden condition), for the transcript + summary."""

    label: str
    action: str                     # human-readable: which countermeasure it chose
    chosen_modcod: str | None       # highest-efficiency rung meeting target, or None (no link)
    center_freq_hz: float
    hop_rate_hz: float | None       # set iff the agent is hopping
    ber: float
    snr_db: float
    meets: bool
    runs: int                       # physical link trials (run_flowgraph) spent this epoch

    @property
    def bits_per_sym(self) -> int:
        return BITS_PER_SYM.get(self.chosen_modcod or "", 0)


class AutonomousAgent:
    """Drives the MCP tool surface across changing conditions, choosing AMC / avoidance / hopping
    from measurement alone."""

    def __init__(self, cli, candidates_hz, target_ber: float = 1e-2,
                 ladder=("bpsk", "qpsk", "16qam"), hop_rates=(50.0, 100.0, 200.0, 400.0),
                 confirm_bits: int = 60_000, rescue_factor: float = 4.0, bo_budget: int = 12,
                 say=lambda *a: None):
        self.cli = cli
        self.candidates = [float(f) for f in candidates_hz]
        self.target_ber = target_ber
        self.ladder = tuple(ladder)
        self.hop_rates = sorted(float(r) for r in hop_rates)
        self.confirm_bits = confirm_bits
        self.rescue_factor = rescue_factor      # BO-rescue a rung only if BER within this x target
        self.bo_budget = bo_budget
        self.say = say
        self.center_freq = self.candidates[0]
        self._hopping = False
        self._runs = 0

    # -- setup: discover + claim, once for the whole mission ----------------------------------
    def setup(self) -> None:
        exp = self.cli.call("describe_experiment", name="link_adaptive")
        self.say(f"[discovery] {exp['success']}")
        devs = self.cli.call("list_devices")
        tx, rx = devs[0]["device_id"], devs[1]["device_id"]
        for dev, role in ((tx, "transmitter"), (rx, "receiver")):
            try:
                self.cli.call("claim_device", device_id=dev, role=role)
            except MCPToolError as exc:
                if exc.code != "claim_denied":   # tolerate ONLY an already-claimed re-run; a real
                    raise                        # failure (unknown device / bad role) must surface

        self.cli.call("set_center_freq", center_freq_hz=self.center_freq)
        self.say(f"  claimed {tx}=tx {rx}=rx; link on {self.center_freq/1e6:.0f} MHz "
                 f"(grader framework-reserved)\n")

    # -- one physical trial -------------------------------------------------------------------
    def _probe(self) -> dict:
        self.cli.call("run_flowgraph", n_bits=self.confirm_bits)
        self._runs += 1
        return self.cli.call("get_metrics")

    def _amc_search(self) -> dict | None:
        """Climb the ladder on the current channel; keep the highest-efficiency rung meeting target,
        rescuing a marginal rung with the BO inner loop. None if nothing meets."""
        from gr_autopilot.control.objective import parse_modcod, spectral_efficiency
        best = None
        for modcod in self.ladder:
            mod, coding = parse_modcod(modcod)   # handle composite 'mod:coding' rungs, not just bare mods
            self.cli.call("build_flowgraph", spec=FlowgraphSpec.link(mod, coding=coding).to_dict())
            m = self._probe()
            if self.target_ber < m["BER"] <= self.rescue_factor * self.target_ber:
                self.cli.call("start_bo_run", params=["phase_correction_rad"], bounds=_PHASE_BOUNDS,
                              target_ber=self.target_ber, budget=self.bo_budget)
                m = self._probe()
            # select by spectral efficiency explicitly (robust to ladder order), not "last meeting rung"
            if m["BER"] <= self.target_ber:
                eff = spectral_efficiency(modcod)
                if best is None or eff > best["eff"]:
                    best = {"modcod": modcod, "ber": m["BER"], "snr": m["SNR"], "eff": eff}
        return best

    # -- one epoch: probe, diagnose, apply the right countermeasure ---------------------------
    def adapt(self, label: str) -> EpochResult:
        r0 = self._runs

        # If we were hopping, first check whether the threat has cleared (drop to one channel).
        if self._hopping:
            self.cli.call("clear_hop_plan")
            self._hopping = False
            best = self._amc_search()
            if best:
                self.say("  threat gone — dropped hopping, back on a single channel")
                return self._result(label, "threat cleared → AMC", best, r0)
            self.say("  still jammed after dropping hop — will re-hop")

        # 1. AMC on the current channel (handles SNR climb/drop by itself).
        best = self._amc_search()
        if best:
            return self._result(label, "AMC (climb/hold)", best, r0)

        # 2. Broken at every rung -> look for a jammer to avoid.
        occ = self.cli.call("sense_spectrum", freqs_hz=self.candidates)
        visible = [o for o in occ if o["occupied"]]
        clear = [o["center_freq_hz"] for o in occ
                 if not o["occupied"] and o["center_freq_hz"] != self.center_freq]
        if visible:
            self.say(f"  sensed a jammer at {visible[0]['center_freq_hz']/1e6:.0f} MHz "
                     f"(+{visible[0]['power_db']:.0f} dB) → frequency avoidance")
            for f in clear:
                self.cli.call("set_center_freq", center_freq_hz=f)
                self.center_freq = f
                best = self._amc_search()
                if best:
                    self.say(f"  retuned to {f/1e6:.0f} MHz — link restored")
                    return self._result(label, "frequency avoidance → AMC", best, r0)

        # 3. Sensed clear yet still broken -> reactive follower -> hop and escalate.
        self.say("  every channel senses clear but the link is broken → reactive jammer "
                 "→ escalate hop rate")
        for rate in self.hop_rates:
            self.cli.call("set_hop_plan", channels_hz=self.candidates, hop_rate_hz=rate)
            best = self._amc_search()
            if best:
                self._hopping = True
                self.say(f"  hopping at {rate:.0f} Hz out-runs the follower — link restored")
                return self._result(label, f"hopping @ {rate:.0f} Hz → AMC", best, r0,
                                    hop_rate=rate)
        self.cli.call("clear_hop_plan")
        return self._result(label, "no countermeasure worked (SNR-limited)", None, r0)

    def _result(self, label, action, best, r0, hop_rate=None) -> EpochResult:
        runs = self._runs - r0
        if best:
            return EpochResult(label, action, best["modcod"], self.center_freq, hop_rate,
                               best["ber"], best["snr"], True, runs)
        return EpochResult(label, action, None, self.center_freq, None, 1.0, 0.0, False, runs)
