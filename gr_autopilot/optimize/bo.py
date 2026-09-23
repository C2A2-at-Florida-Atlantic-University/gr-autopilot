"""Gaussian-process Bayesian optimization (minimization) with ask/tell + early stop.

Small and legible on purpose (spec §5, "legibility over cleverness"): an isotropic-RBF GP
fit by Cholesky, expected-improvement acquisition maximized by random candidate sampling.
Designed for the low-dimensional (4-6 knob), small-budget regime where each evaluation is an
expensive hardware trial. The objective is MINIMIZED; callers map "maximize spectral
efficiency subject to BER" to a scalar loss.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.special import erf


def _normal_cdf(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + erf(z / np.sqrt(2.0)))


def _normal_pdf(z: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * z * z) / np.sqrt(2.0 * np.pi)


class _GP:
    """Isotropic-RBF Gaussian process over inputs already scaled to the unit cube."""

    def __init__(self, lengthscale: float = 0.2, signal_var: float = 1.0, noise: float = 1e-6):
        self.l = lengthscale
        self.sf2 = signal_var
        self.noise = noise
        self._X = None
        self._L = None
        self._alpha = None
        self._ymean = 0.0
        self._ystd = 1.0

    def _kernel(self, A: np.ndarray, B: np.ndarray) -> np.ndarray:
        d2 = np.sum(A**2, 1)[:, None] + np.sum(B**2, 1)[None, :] - 2.0 * A @ B.T
        d2 = np.maximum(d2, 0.0)
        return self.sf2 * np.exp(-0.5 * d2 / (self.l**2))

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_GP":
        self._X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        self._ymean = float(y.mean())
        self._ystd = float(y.std()) or 1.0
        yn = (y - self._ymean) / self._ystd
        K = self._kernel(self._X, self._X)
        jitter = self.noise
        for _ in range(6):  # escalate jitter until Cholesky succeeds
            try:
                self._L = np.linalg.cholesky(K + jitter * np.eye(len(self._X)))
                break
            except np.linalg.LinAlgError:
                jitter *= 10
        else:
            # last resort AFTER the loop's escalation (jitter is now larger than every value tried),
            # so it is strictly stronger than the jitters that just failed — not a weaker 1e-3.
            self._L = np.linalg.cholesky(K + jitter * np.eye(len(self._X)))
        self._alpha = np.linalg.solve(self._L.T, np.linalg.solve(self._L, yn))
        return self

    def predict(self, Xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        Ks = self._kernel(self._X, Xs)
        mu = Ks.T @ self._alpha
        v = np.linalg.solve(self._L, Ks)
        var = self.sf2 - np.sum(v**2, axis=0)
        var = np.maximum(var, 1e-12)
        mu = mu * self._ystd + self._ymean
        std = np.sqrt(var) * self._ystd
        return mu, std


class BayesianOptimizer:
    """Minimizing GP-BO with an ask/tell interface."""

    def __init__(self, bounds, n_init: int = 5, seed: int = 0, xi: float = 0.01,
                 n_candidates: int = 2000, lengthscale: float = 0.2):
        self.bounds = np.asarray(bounds, dtype=float)
        self.dim = len(self.bounds)
        self.lo = self.bounds[:, 0]
        self.hi = self.bounds[:, 1]
        self.span = np.where(self.hi > self.lo, self.hi - self.lo, 1.0)
        self.n_init = max(1, n_init)
        self.xi = xi
        self.n_candidates = n_candidates
        self.lengthscale = lengthscale
        self._rng = np.random.default_rng(seed)
        self._X: list[np.ndarray] = []
        self._y: list[float] = []

    def _to_unit(self, X: np.ndarray) -> np.ndarray:
        return (np.asarray(X, dtype=float) - self.lo) / self.span

    def _from_unit(self, U: np.ndarray) -> np.ndarray:
        return self.lo + np.asarray(U, dtype=float) * self.span

    @property
    def n_trials(self) -> int:
        return len(self._y)

    def ask(self) -> list[float]:
        if len(self._X) < self.n_init:
            return self._from_unit(self._rng.random(self.dim)).tolist()

        Xu = self._to_unit(np.array(self._X))
        y = np.array(self._y)
        gp = _GP(lengthscale=self.lengthscale).fit(Xu, y)

        # Candidate pool: uniform samples + local jitter around the current best.
        cand = self._rng.random((self.n_candidates, self.dim))
        best_u = self._to_unit(self._X[int(np.argmin(y))])
        local = np.clip(best_u + 0.05 * self._rng.standard_normal((self.n_candidates // 4, self.dim)),
                        0.0, 1.0)
        cand = np.vstack([cand, local])

        mu, std = gp.predict(cand)
        f_best = float(y.min())
        # Scale the exploration margin to the observed objective spread. A FIXED absolute xi (0.01)
        # swamps a tiny objective — e.g. bo_loss ~1e-3 at a BER target — driving (f_best - xi) < 0 so
        # EI collapses below 1e-12 and ask() degenerates to random search exactly where GP guidance is
        # wanted. Scaling keeps xi a constant *fraction* of the range at any magnitude.
        xi = self.xi * float(y.max() - y.min())
        z = (f_best - xi - mu) / std
        ei = (f_best - xi - mu) * _normal_cdf(z) + std * _normal_pdf(z)
        ei = np.where(std > 1e-12, ei, 0.0)
        if float(ei.max()) <= 1e-12:  # acquisition flat -> explore randomly
            return self._from_unit(self._rng.random(self.dim)).tolist()
        return self._from_unit(cand[int(np.argmax(ei))]).tolist()

    def tell(self, x, y: float) -> None:
        self._X.append(np.asarray(x, dtype=float))
        self._y.append(float(y))

    def best(self) -> tuple[list[float], float]:
        if not self._y:
            raise ValueError("no observations yet")
        i = int(np.argmin(self._y))
        return self._X[i].tolist(), self._y[i]


@dataclass
class BoStatus:
    trials: int
    best_x: list
    best_y: float
    history: list = field(default_factory=list)   # list of (x, y)
    stopped_early: bool = False
    stop_reason: str = ""


def run_bo(objective, bounds, budget: int, n_init: int = 5, seed: int = 0,
           early_stop=None) -> BoStatus:
    """Drive an ask/tell BO loop over ``objective`` for ``budget`` trials.

    ``objective(x) -> float`` is minimized. ``early_stop(status) -> (bool, reason)`` is
    called after each trial; returning True halts the run (the LLM interrupt of §4.3).
    """
    opt = BayesianOptimizer(bounds, n_init=n_init, seed=seed)
    history = []
    stopped, reason = False, ""
    for _ in range(budget):
        x = opt.ask()
        y = float(objective(x))
        opt.tell(x, y)
        history.append((list(x), y))
        if early_stop is not None:
            bx, by = opt.best()
            status = BoStatus(opt.n_trials, bx, by, list(history))
            do_stop, why = early_stop(status)
            if do_stop:
                stopped, reason = True, why
                break
    bx, by = opt.best()
    return BoStatus(opt.n_trials, bx, by, history, stopped_early=stopped, stop_reason=reason)
