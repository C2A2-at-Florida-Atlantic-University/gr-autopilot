"""The two-loop control policy (spec §4).

A slow structural outer loop (which modulation/coding) wraps a fast Bayesian-optimization
inner loop (continuous knobs within a fixed structure). Stage 1 ships a REFERENCE
(non-LLM) controller so the whole closed loop — structural search + BO tuning + selection
under a BER-constrained spectral-efficiency objective — runs and is testable in simulation
before the LLM drives it via MCP and before hardware is attached.
"""
from gr_autopilot.control.amc import AmcCell, amc_sweep
from gr_autopilot.control.controller import ControllerResult, TwoLoopController
from gr_autopilot.control.objective import bo_loss, choose_modcod, spectral_efficiency

__all__ = [
    "TwoLoopController",
    "ControllerResult",
    "AmcCell",
    "amc_sweep",
    "bo_loss",
    "choose_modcod",
    "spectral_efficiency",
]
