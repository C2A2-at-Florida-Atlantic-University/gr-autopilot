"""Bayesian-optimization tests (no hardware): does it actually find minima?"""
import numpy as np
import pytest

from gr_autopilot.optimize import BayesianOptimizer, run_bo


def test_converges_1d_quadratic():
    # minimize (x - 0.3)^2 on [-2, 2]
    res = run_bo(lambda x: (x[0] - 0.3) ** 2, [(-2.0, 2.0)], budget=25, seed=1)
    assert abs(res.best_x[0] - 0.3) < 0.15
    assert res.best_y < 0.02


def test_converges_2d_quadratic():
    def f(x):
        return (x[0] - 0.5) ** 2 + (x[1] + 0.4) ** 2
    res = run_bo(f, [(-1.0, 1.0), (-1.0, 1.0)], budget=40, seed=2)
    assert res.best_y < 0.03


def test_beats_random_search_on_average():
    # BO should reach a lower minimum than pure random search at the same budget.
    def f(x):
        return float(np.sum((np.array(x) - np.array([0.2, -0.3, 0.1])) ** 2))
    bounds = [(-1.0, 1.0)] * 3
    budget = 40
    bo = run_bo(f, bounds, budget=budget, seed=3).best_y
    rng = np.random.default_rng(3)
    rand = min(f(rng.uniform(-1, 1, 3)) for _ in range(budget))
    assert bo <= rand


def test_beats_random_on_a_tiny_objective():
    # regression for the xi-scaling bug: at BER-scale (~1e-3) a FIXED absolute xi=0.01 swamped the
    # objective, collapsed EI, and BO degenerated to random search. Scaled xi keeps it GP-guided even
    # when the objective is tiny.
    def f(x):
        return 1e-3 * float((x[0] - 0.2) ** 2 + (x[1] + 0.3) ** 2)
    bounds = [(-1.0, 1.0)] * 2
    budget = 30
    bo = run_bo(f, bounds, budget=budget, seed=5).best_y
    rng = np.random.default_rng(5)
    rand = min(f(rng.uniform(-1, 1, 2)) for _ in range(budget))
    assert bo <= rand


def test_early_stop_interrupt():
    calls = {"n": 0}

    def f(x):
        calls["n"] += 1
        return (x[0]) ** 2 + 5.0  # never gets near a target of, say, 0.1

    def early(status):
        # LLM-style interrupt: after 8 trials, if best is still far from target, stop.
        if status.trials >= 8 and status.best_y > 0.1:
            return True, "hopeless: best still >> target after 8 trials"
        return False, ""

    res = run_bo(f, [(-2.0, 2.0)], budget=100, seed=0, early_stop=early)
    assert res.stopped_early
    assert res.trials == 8
    assert "hopeless" in res.stop_reason


def test_ask_tell_manual_and_deterministic():
    def make():
        opt = BayesianOptimizer([(-1.0, 1.0)], n_init=4, seed=7)
        for _ in range(12):
            x = opt.ask()
            opt.tell(x, (x[0] - 0.25) ** 2)
        return opt.best()
    a = make()
    b = make()
    assert a[0] == pytest.approx(b[0])  # same seed -> same trajectory
    assert abs(a[0][0] - 0.25) < 0.2


def test_ask_respects_bounds():
    opt = BayesianOptimizer([(3.0, 5.0), (-10.0, -8.0)], n_init=3, seed=0)
    for _ in range(15):
        x = opt.ask()
        assert 3.0 <= x[0] <= 5.0 and -10.0 <= x[1] <= -8.0
        opt.tell(x, x[0] + x[1])
