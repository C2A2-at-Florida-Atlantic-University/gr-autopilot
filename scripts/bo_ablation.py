#!/usr/bin/env python3
"""BO sample-efficiency ablation (spec §4.2 / §10): the inner loop uses Bayesian optimization
because each evaluation is an expensive hardware trial (~1-2 s), so reaching the optimum in
FEW trials is the whole point. This quantifies BO vs random search (best-so-far loss vs number
of trials, averaged over optimizer seeds) on:

  (A) the real sim inner-loop task -- tune the receiver phase correction to undo a hidden
      carrier-phase offset that would otherwise wreck 16-QAM;
  (B) a multi-knob loop-tuning proxy (a narrow Gaussian "sweet spot" in D dims, the regime the
      design targets: 4-6 continuous knobs with one good operating point) at D = 1, 3, 5.

Pure sim / synthetic -- fast and reproducible, no radios.

Run:  python scripts/bo_ablation.py
"""
from __future__ import annotations

import numpy as np

from gr_autopilot.control.objective import bo_loss
from gr_autopilot.flowgraph import FlowgraphSpec, build_and_run
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.optimize import run_bo
from gr_autopilot.scoring.metrics import compute_metrics

BUDGET = 24
SEEDS = 24


def random_search(objective, bounds, budget, seed):
    rng = np.random.default_rng(seed)
    lo = np.array([b[0] for b in bounds]); hi = np.array([b[1] for b in bounds])
    ys = [float(objective((lo + rng.random(len(bounds)) * (hi - lo)).tolist())) for _ in range(budget)]
    return np.minimum.accumulate(ys)


def bo_search(objective, bounds, budget, seed):
    st = run_bo(objective, bounds, budget=budget, seed=seed)
    return np.minimum.accumulate([y for _, y in st.history])


def sequential_search(objective, bounds, budget, seed):
    """Naive one-knob-at-a-time (coordinate) search: cycle the knobs, grid-sweeping each around
    the running best. A proxy for tuning parameters sequentially without a joint model -- it
    misses cross-knob interactions, which is exactly where a joint optimizer wins."""
    rng = np.random.default_rng(seed)
    lo = np.array([b[0] for b in bounds]); hi = np.array([b[1] for b in bounds])
    dim = len(bounds)
    best_x = lo + rng.random(dim) * (hi - lo)
    best_y = float(objective(best_x.tolist())); ys = [best_y]; d = 0
    while len(ys) < budget:
        for v in np.linspace(lo[d], hi[d], 4):
            if len(ys) >= budget:
                break
            cand = best_x.copy(); cand[d] = v
            y = float(objective(cand.tolist())); ys.append(y)
            if y < best_y:
                best_y, best_x = y, cand.copy()
        d = (d + 1) % dim
    return np.minimum.accumulate(ys)


def compare(objective, bounds, label):
    bo = np.mean([bo_search(objective, bounds, BUDGET, s) for s in range(SEEDS)], axis=0)
    rnd = np.mean([random_search(objective, bounds, BUDGET, 1000 + s) for s in range(SEEDS)], axis=0)
    seq = np.mean([sequential_search(objective, bounds, BUDGET, 2000 + s) for s in range(SEEDS)], axis=0)
    marks = [3, 6, 9, 12, 18, 24]
    print(f"\n[{label}]  mean best-so-far loss vs trials (avg of {SEEDS} seeds):")
    print("   trials     : " + " ".join(f"{t:>8d}" for t in marks))
    print("   BO         : " + " ".join(f"{bo[t-1]:>8.4f}" for t in marks))
    print("   sequential : " + " ".join(f"{seq[t-1]:>8.4f}" for t in marks))
    print("   random     : " + " ".join(f"{rnd[t-1]:>8.4f}" for t in marks))
    target = min(rnd[-1], seq[-1])  # BO to match the better naive baseline's final result
    hit = next((i + 1 for i, v in enumerate(bo) if v <= target), None)
    if hit:
        print(f"   -> BO matches the better naive baseline's {BUDGET}-trial result in {hit} trials "
              f"({BUDGET / hit:.1f}x fewer)")


# (A) real sim inner-loop task: undo a hidden phase offset that breaks 16-QAM
be = NumpySimBackend()
SPEC = FlowgraphSpec.link("16qam")

def radio_objective(x):
    r = build_and_run(SPEC, be, n_payload_bits=20_000, es_n0_db=18.0,
                      phase_offset_rad=0.5, phase_correction_rad=float(x[0]), seed=0)
    return bo_loss(compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms))

compare(radio_objective, [(-0.7854, 0.7854)], "A: 16-QAM phase-correction (1 knob, real sim link)")

# (B) multi-knob loop-tuning proxy: a narrow sweet spot in D dims (per-seed random optimum)
def make_valley(dim, width=0.16):
    opt = np.full(dim, 0.5) + 0.2 * np.sin(np.arange(dim) + 1)  # fixed, interior optimum
    def f(x):
        x = np.asarray(x, dtype=float)
        return float(1.0 - np.exp(-np.sum((x - opt) ** 2) / (2 * width ** 2)))
    return f

for dim in (1, 3, 5):
    compare(make_valley(dim), [(0.0, 1.0)] * dim, f"B: loop-tuning sweet spot ({dim} knobs)")
