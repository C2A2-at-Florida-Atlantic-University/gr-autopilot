"""Co-channel interference model for Stage-2 link-under-interference experiments.

Framework-owned physics. An interferer (CW tone or band-limited noise) contributes extra power
at the receiver; the fraction that lands inside the link's passband is set by the spectral
overlap between the interferer and the (agent-controllable) link center frequency. Move the link
off the interferer and the overlap -- and thus the in-band interference -- drops toward zero, so
BER recovers. That is the "frequency avoidance" countermeasure the agent must discover.

Pure numpy, backend-agnostic: the same overlap math drives the simulation channel and the
book-keeping/analysis for the hardware (HackRF) experiment.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Interferer:
    """A co-channel interferer with a center frequency, occupied bandwidth, and strength.

    ``inr_db`` is the in-band interference-to-noise ratio at FULL spectral overlap (``frac == 1``,
    the interferer entirely inside the link passband) relative to the link's thermal noise. Note this
    is not the same as "the link sits on it": a wide ``"noise"`` jammer only partially overlaps even
    when co-centred, so its realized in-band INR is ``inr_db + 10*log10(frac) < inr_db``. ``kind`` is
    ``"cw"`` (a tone; bandwidth ignored) or ``"noise"`` (band-limited, power over ``bandwidth_hz``).
    """

    center_freq_hz: float
    inr_db: float = 20.0
    kind: str = "cw"
    bandwidth_hz: float = 0.0
    reactive: bool = False   # fires only when the link transmits, and follows its frequency
    reaction_s: float = 0.0  # a reactive jammer's detect-and-retune latency (0 = instant, unbeatable
                             # by hopping; see link/hopping.py). Ignored for a fixed jammer.

    def in_band_fraction(self, link_center_hz: float, link_bw_hz: float) -> float:
        """Fraction of the interferer's power inside the link passband, in [0, 1]."""
        half = link_bw_hz / 2.0
        if self.kind == "cw" or self.bandwidth_hz <= 0.0:
            return 1.0 if abs(self.center_freq_hz - link_center_hz) <= half else 0.0
        lo = max(link_center_hz - half, self.center_freq_hz - self.bandwidth_hz / 2.0)
        hi = min(link_center_hz + half, self.center_freq_hz + self.bandwidth_hz / 2.0)
        return max(0.0, hi - lo) / self.bandwidth_hz

    def effective_es_n0_db(self, link_center_hz: float, link_bw_hz: float,
                           clean_es_n0_db: float) -> float:
        """Link Es/(N0+I) once the in-band interference is folded into the noise.

        With no spectral overlap the link is unchanged; at full overlap the noise floor rises by
        ~``inr_db`` and the effective SNR collapses. This is what the grader would measure as the
        link degrades and (after the agent retunes away) recovers.
        """
        # A reactive jammer detects the link's transmission and jams THAT frequency, so moving
        # the link doesn't escape it -- the overlap is effectively total wherever the link goes.
        frac = 1.0 if self.reactive else self.in_band_fraction(link_center_hz, link_bw_hz)
        n0 = 10.0 ** (-clean_es_n0_db / 10.0)              # thermal, rel. to Es=1
        i_full = n0 * 10.0 ** (self.inr_db / 10.0)          # interference at full overlap
        denom = n0 + i_full * frac
        return float(-10.0 * np.log10(denom))

    def sensed_power_db(self, rx_center_hz: float, rx_bw_hz: float) -> float:
        """Received power over the thermal-noise floor (dB) that the MONITOR role sees if it
        tunes to ``rx_center`` with ``rx_bw`` bandwidth and the link is silent -- i.e. energy
        detection. The interferer shows as a peak at its frequency; elsewhere it reads ~0 dB
        (just noise). This is how the agent *senses* where the jammer is instead of blindly
        probing every channel with a full link trial.
        """
        frac = self.in_band_fraction(rx_center_hz, rx_bw_hz)
        return float(10.0 * np.log10(1.0 + 10.0 ** (self.inr_db / 10.0) * frac))
