"""The Bayesian-optimization inner loop (spec §4.2).

A dependency-free GP-surrogate optimizer with an ask/tell interface (the shape a hardware
loop wants: propose a point, run a trial, report the metric) and an explicit early-stop
hook for the LLM interrupt (spec §4.3). Self-contained numpy/scipy — no scikit-optimize.
"""
from gr_autopilot.optimize.bo import BayesianOptimizer, BoStatus, run_bo

__all__ = ["BayesianOptimizer", "BoStatus", "run_bo"]
