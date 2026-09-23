"""SkillAuthor — the agent discovers a new building block on the grader and promotes it (test-gated).

The authoring loop: benchmark a ladder of candidate modcods (including *composite* coded rungs) at the
current channel; the highest-efficiency rung that MEETS the BER target is the discovery. If that rung
is a composite the base (single-modulation, uncoded) rungs cannot match — e.g. at a marginal SNR where
uncoded 16-QAM fails and QPSK wastes efficiency, coded 16-QAM (3.0 bits/sym) fills the gap — it is
proposed as a **learned skill**. Promotion is **test-gated**: the proposal must re-validate on a fresh
trial and beat its baseline efficiency by a margin before it is persisted into the agent's vocabulary.

All measurement flows through the framework grader; the agent never scores its own promotion.
"""
from __future__ import annotations

from dataclasses import dataclass

from gr_autopilot.control.objective import parse_modcod, spectral_efficiency
from gr_autopilot.flowgraph.learned import LearnedSkill, LearnedSkillStore
from gr_autopilot.flowgraph.spec import FlowgraphSpec
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring.metrics import compute_metrics


@dataclass
class GateResult:
    ok: bool
    reason: str


class SkillAuthor:
    def __init__(self, backend, target_ber: float = 1e-2, bits: int = 40_000, sps: int = 8,
                 rolloff: float = 0.35, seed: int = 0, channel_kwargs: dict | None = None,
                 repeats: int = 3):
        self.backend = backend
        self.target_ber = target_ber
        self.bits = bits
        self.sps = sps
        self.rolloff = rolloff
        self.seed = seed
        self.repeats = repeats
        #: The measured ladder from the last :meth:`author` call, kept so a run that authors
        #: nothing can say WHY -- "no composite rung beats the base rungs" is not actionable, and
        #: the useful answer is which rungs met the target and which did not.
        self.last_bench: dict = {}
        # for backends that read the condition from LinkParams (sim NumpySim); {} for owns_channel
        # backends, which carry the hidden condition as device/model state.
        self.channel_kwargs = dict(channel_kwargs or {})

    def _bench(self, modcod: str, seed: int) -> tuple[float, float, float]:
        """Run one modcod at the backend's current (hidden) condition -> (ber, snr_est, efficiency).

        Averaged over ``self.repeats`` measurements. A single run decides a rung's fate here, and on
        radios one run is noisy enough to flip a marginal rung either way -- which is exactly the
        rung this loop exists to find, since a coded candidate only wins in the narrow window where
        the uncoded rung above it is failing.
        """
        mod, coding = parse_modcod(modcod)
        bers, snrs = [], []
        for i in range(max(1, int(self.repeats))):
            r = self.backend.run_link(LinkParams(modulation=mod, coding=coding,
                                                 n_payload_bits=self.bits, sps=self.sps,
                                                 rolloff=self.rolloff, seed=seed + i,
                                                 **self.channel_kwargs))
            m = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
            bers.append(m.ber)
            snrs.append(m.snr_db)
        return (sum(bers) / len(bers), sum(snrs) / len(snrs), spectral_efficiency(modcod))

    def author(self, name: str, ladder) -> LearnedSkill | None:
        """Benchmark ``ladder`` at the current channel; propose the highest-efficiency MEETING rung as a
        learned skill iff it is a composite (coded) rung that beats every base (uncoded) rung. Returns
        ``None`` when the best is already a base rung (nothing new worth authoring)."""
        res = {mc: self._bench(mc, self.seed) for mc in ladder}
        self.last_bench = {mc: {"ber": v[0], "snr_est": round(v[1], 2), "efficiency": v[2],
                                "meets": bool(v[0] <= self.target_ber)}
                           for mc, v in res.items()}
        meeting = {mc: v for mc, v in res.items() if v[0] <= self.target_ber}
        if not meeting:
            return None
        best = max(meeting, key=lambda mc: meeting[mc][2])
        base = [mc for mc in meeting if ":" not in mc]
        best_base = max(base, key=lambda mc: meeting[mc][2]) if base else None
        best_eff = meeting[best][2]
        base_eff = meeting[best_base][2] if best_base else 0.0
        if ":" not in best or best_eff <= base_eff:
            return None                              # the best is a base rung -> nothing to author
        mod, coding = parse_modcod(best)
        spec = FlowgraphSpec.link(mod, sps=self.sps, rolloff=self.rolloff, coding=coding).to_dict()
        spec["structure_id"] = name
        return LearnedSkill(
            name=name, spec=spec,
            objective=f"max spectral efficiency meeting BER<={self.target_ber:g}",
            benchmark={"modcod": best, "efficiency": best_eff, "ber": meeting[best][0],
                       "snr_est": round(meeting[best][1], 2), "baseline": best_base,
                       "baseline_efficiency": base_eff, "gain": round(best_eff - base_eff, 2)},
            parent=best_base or "")

    def promote(self, proposal: LearnedSkill | None, store: LearnedSkillStore,
                min_gain: float = 0.4) -> GateResult:
        """Test-gate + persist. The proposal must re-validate on a FRESH trial (not a lucky one) and
        beat its baseline efficiency by ``min_gain``. On pass it is added to ``store`` (persisted)."""
        if proposal is None:
            return GateResult(False, "no proposal to promote")
        if proposal.name in store:
            return GateResult(False, f"{proposal.name!r} already in the vocabulary")
        gain = float(proposal.benchmark.get("gain", 0.0))
        if gain < min_gain:
            return GateResult(False, f"gate: efficiency gain {gain:g} < {min_gain:g} over baseline")
        ber, _, _ = self._bench(proposal.benchmark["modcod"], self.seed + 12345)  # independent trial
        if ber > self.target_ber:
            return GateResult(False, f"gate: re-run BER {ber:.2e} > target {self.target_ber:g}")
        store.add(proposal)
        return GateResult(True, f"promoted {proposal.name!r} (+{gain:g} bits/sym over {proposal.parent})")
