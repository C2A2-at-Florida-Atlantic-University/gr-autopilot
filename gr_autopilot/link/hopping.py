"""Frequency-hopping model (Stage 3) — the countermeasure to a REACTIVE jammer.

Stage 2 characterized an honest limit: a *reactive* jammer (idle while the link is silent, so
energy detection sees the band clear, then following the link when it transmits) defeats
frequency *avoidance* — a sensed-clean channel still gets jammed. The answer is frequency
*hopping*: change center frequency every dwell ``T = 1/hop_rate`` across a hop set, faster than the
jammer can detect-and-retune.

Against a jammer the time-averaged in-band interference collapses to a **jammed fraction** the link
controls by its hop rate:

* **fixed** jammer (blind / protocol-unaware): only the hops that land in its band are hit, so the
  jammed fraction is ``(occupied channels) / (hop-set size)`` — hopping spreads the risk.
* **reactive** jammer with reaction latency ``tau``: it can only jam a hop *after* ``tau`` of the
  dwell, so the jammed fraction is ``max(0, 1 - tau/T) = max(0, 1 - tau*hop_rate)``. Hop faster than
  the jammer reacts (``T < tau``) and it always jams the frequency the link just *left* → jammed
  fraction 0, evasion. **The honest limit:** a jammer with small enough ``tau`` (an FPGA follower)
  out-reacts any achievable hop rate.

``jammed_fraction`` and ``hopped_effective_es_n0_db`` are the analytic summary (a single averaged
Es/(N0+I)). Grading, however, is done PER-DWELL: ``InterferenceSimBackend`` splits the payload into a
jammed population (at the on-channel interference SNR) and a clean population and concatenates the
graded bits — because BER is nonlinear in noise power, that per-dwell average is the physically
correct one and is what the hardware path (``HoppingBackend``) computes, so the two agree. The scalar
here is a summary for telemetry/analysis, not the grade.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from gr_autopilot.link.interference import Interferer


@dataclass(frozen=True)
class HopPlan:
    """A frequency-hopping schedule: a hop set and how fast the link cycles it."""

    channels: tuple[float, ...]   # hop-set center frequencies (Hz)
    hop_rate_hz: float            # hops per second — the agent's evasion knob

    @property
    def dwell_s(self) -> float:
        return 1.0 / self.hop_rate_hz


def jammed_fraction(jammer: Interferer | None, hop_plan: HopPlan, link_bw_hz: float) -> float:
    """Time-averaged fraction of the hopped transmission the jammer actually hits (in [0, 1])."""
    if jammer is None:
        return 0.0
    if jammer.reactive:
        # follows the link, but only catches each dwell AFTER its reaction latency
        return float(max(0.0, 1.0 - jammer.reaction_s * hop_plan.hop_rate_hz))
    # fixed jammer: the fraction of the hop set whose passband its energy overlaps
    hit = sum(1 for f in hop_plan.channels if jammer.in_band_fraction(f, link_bw_hz) > 0.0)
    return hit / max(1, len(hop_plan.channels))


def hopped_effective_es_n0_db(jammer: Interferer | None, hop_plan: HopPlan, link_bw_hz: float,
                              clean_es_n0_db: float) -> float:
    """Effective Es/(N0+I) of a hopped link: the jammed fraction folded into the noise floor —
    the same model as Interferer.effective_es_n0_db, with the time-averaged hop fraction."""
    if jammer is None:
        return clean_es_n0_db
    f = jammed_fraction(jammer, hop_plan, link_bw_hz)
    n0 = 10.0 ** (-clean_es_n0_db / 10.0)
    i_full = n0 * 10.0 ** (jammer.inr_db / 10.0)
    return float(-10.0 * np.log10(n0 + i_full * f))


def min_evading_hop_rate_hz(jammer: Interferer | None) -> float | None:
    """The hop rate above which a reactive jammer is fully out-hopped (jammed fraction 0): 1/tau.
    None for a non-reactive jammer (hopping helps by spreading, not by out-reacting) or tau<=0."""
    if jammer is None or not jammer.reactive or jammer.reaction_s <= 0.0:
        return None
    return 1.0 / jammer.reaction_s
