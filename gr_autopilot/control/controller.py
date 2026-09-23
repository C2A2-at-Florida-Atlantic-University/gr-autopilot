"""Reference two-loop controller (spec §4): structural outer loop + BO inner loop.

For a given (hidden) channel condition, the controller:
  outer loop  — walks the modulation ladder (structural choice);
  inner loop  — for each rung, runs Bayesian optimization over continuous knobs
                (e.g. receiver phase correction) to reach that structure's best BER;
  selection   — keeps the highest-spectral-efficiency rung whose BER meets the target.

It is NOT told the channel SNR: it passes the operator/framework channel settings straight
to the backend and bases every decision only on measured BER/EVM (spec §10.1). This is the
non-LLM reference that later gets replaced by the LLM driving the same tools over MCP.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from gr_autopilot.control.objective import bo_loss, choose_modcod, parse_modcod
from gr_autopilot.flowgraph import FlowgraphSpec, build_and_run
from gr_autopilot.ledger import EditLedger, LedgerEntry
from gr_autopilot.link.backend import LinkBackend
from gr_autopilot.optimize import run_bo
from gr_autopilot.scoring.metrics import Metrics, compute_metrics

DEFAULT_LADDER = ("bpsk", "qpsk", "16qam")
# Continuous knob(s) the inner loop tunes, with bounds (spec §4.2: 4-6 knobs, small range).
DEFAULT_KNOBS = {"phase_correction_rad": (-math.pi / 4, math.pi / 4)}


@dataclass
class ControllerResult:
    chosen: str
    per_mod: dict = field(default_factory=dict)     # modulation -> Metrics (best tuned)
    best_knobs: dict = field(default_factory=dict)  # modulation -> knob values
    trajectory: list = field(default_factory=list)  # per-rung summary
    iterations: int = 0


class TwoLoopController:
    def __init__(self, backend: LinkBackend, target_ber: float = 1e-3,
                 ladder=DEFAULT_LADDER, knobs: dict | None = None,
                 bo_budget: int = 12, trial_bits: int = 50_000, confirm_bits: int = 200_000,
                 ledger: EditLedger | None = None, seed: int = 0,
                 sps: int = 4, rolloff: float = 0.35, inner_loss=None):
        self.backend = backend
        self.target_ber = target_ber
        self.ladder = tuple(ladder)
        # None -> the default inner loop (phase-correction BO); an explicit {} -> no inner loop
        # (outer-loop AMC only). NB: `knobs or DEFAULT_KNOBS` would be wrong here -- an empty dict
        # is falsy, so it would silently re-enable BO when a caller asked for none.
        self.knobs = DEFAULT_KNOBS if knobs is None else dict(knobs)
        self.bo_budget = bo_budget
        self.trial_bits = trial_bits
        self.confirm_bits = confirm_bits
        self.ledger = ledger
        self.seed = seed
        # Pulse-shape the built specs carry (the hardware backend needs its tested sps=8;
        # sim backends ignore sps). Kept here so the same controller drives sim and hardware.
        self.sps = sps
        self.rolloff = rolloff
        # Inner-loop loss the BO minimizes: callable(metrics, knobvals) -> float. Default is
        # pure BER (bo_loss); pass a bandwidth-aware loss for spectral-efficiency tuning.
        self.inner_loss = inner_loss or (lambda m, kv: bo_loss(m))

    def _measure(self, spec: FlowgraphSpec, condition: dict, knobvals: dict, n_bits: int, seed: int) -> Metrics:
        r = build_and_run(spec, self.backend, n_payload_bits=n_bits, seed=seed, **condition, **knobvals)
        return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)

    def run(self, condition: dict) -> ControllerResult:
        """condition: the hidden channel (e.g. {'es_n0_db': 12.0, 'phase_offset_rad': 0.3})."""
        knob_names = list(self.knobs)
        bounds = [self.knobs[k] for k in knob_names]
        per_mod: dict[str, Metrics] = {}
        best_knobs: dict[str, dict] = {}
        trajectory = []
        it = 0

        for modcod in self.ladder:
            mod, coding = parse_modcod(modcod)
            spec = FlowgraphSpec.link(mod, sps=self.sps, rolloff=self.rolloff, coding=coding)

            def objective(x, spec=spec):
                kv = dict(zip(knob_names, x))
                m = self._measure(spec, condition, kv, self.trial_bits, self.seed)
                return self.inner_loss(m, kv)

            if knob_names:
                status = run_bo(objective, bounds, budget=self.bo_budget, seed=self.seed)
                tuned = dict(zip(knob_names, status.best_x))
                m = self._measure(spec, condition, tuned, self.confirm_bits, self.seed + 1)
                # Regression guard: BO tunes on `seed` but confirms on `seed+1`, so a knob overfit to
                # trial-seed noise can confirm WORSE than untuned and wrongly drop a boundary rung. Only
                # when the tuned confirm FAILS the target do we also try the untuned baseline ({} -> spec
                # defaults) and keep whichever confirms better — no extra link run on a clean sweep.
                if m.ber > self.target_ber:
                    m0 = self._measure(spec, condition, {}, self.confirm_bits, self.seed + 1)
                    if m0.ber < m.ber:
                        tuned, m = {}, m0
            else:
                tuned = {}
                m = self._measure(spec, condition, tuned, self.confirm_bits, self.seed + 1)
            per_mod[modcod] = m
            best_knobs[modcod] = tuned
            it += 1
            trajectory.append({"modulation": modcod, "ber": m.ber, "evm_pct": m.evm_pct,
                               "snr_db": m.snr_db, "knobs": tuned})
            if self.ledger is not None:
                self.ledger.append(LedgerEntry(
                    iteration=it, structure_id=spec.structure_id,
                    edit_description=f"evaluate {modcod} (BO-tuned)",
                    params={k: round(v, 4) for k, v in tuned.items()},
                    metrics={"BER": m.ber, "EVM": round(m.evm_pct, 2), "SNR_est": round(m.snr_db, 2)},
                    verdict="tuning", loop="inner"))

        chosen = choose_modcod(per_mod, self.target_ber, self.ladder)
        it += 1
        if self.ledger is not None:
            self.ledger.append(LedgerEntry(
                iteration=it, structure_id=f"{chosen}_link",
                edit_description=f"select {chosen} (max spectral efficiency meeting BER<={self.target_ber:g})",
                metrics={"BER": per_mod[chosen].ber}, verdict="kept", loop="outer"))
        return ControllerResult(chosen=chosen, per_mod=per_mod, best_knobs=best_knobs,
                                trajectory=trajectory, iterations=it)
