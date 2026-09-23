"""Band-dependent acquisition envelope: the CFO model and the acquisition ceiling.

The ZC front-end tolerates a fixed CFO range in Hz; the two-Pluto offset is ppm·f_c, so there's a band
ceiling above which acquisition fails independent of SNR. These lock in the closed-form model and that
the real sync.acquire honours it (holds below the knee, breaks above), in pure numpy.
"""
from __future__ import annotations

import numpy as np
import pytest

from gr_autopilot.link import band


def test_cfo_scales_linearly_with_band():
    # CFO(Hz) = ppm·f_c — doubling the band doubles the CFO
    assert band.cfo_hz(1e9, 25) == pytest.approx(25e3)
    assert band.cfo_hz(2e9, 25) == pytest.approx(2 * band.cfo_hz(1e9, 25))
    assert band.cfo_hz(1e9, 0) == 0.0


def test_acquisition_range_and_ceiling_are_consistent():
    Rs = band.symbol_rate_hz()
    rng = band.acquisition_range_hz(Rs, cfo_max=0.15)
    assert rng == pytest.approx(0.15 * Rs)
    # at the ceiling band, the ppm-CFO exactly equals the acquisition range
    for ppm in (10, 25, 40):
        ceil = band.band_ceiling_hz(ppm, Rs, cfo_max=0.15)
        assert band.cfo_hz(ceil, ppm) == pytest.approx(rng, rel=1e-9)
    assert band.band_ceiling_hz(0.0, Rs) == float("inf")   # a perfect LO has no ceiling


def test_acquisition_holds_below_ceiling_breaks_above():
    Rs = band.symbol_rate_hz()
    ppm = 25.0
    ceil = band.band_ceiling_hz(ppm, Rs)          # ~1.56 GHz at the bench config
    below = band.synthetic_band_trial(ceil * 0.6, ppm, es_n0_db=15.0, seed=3)
    above = band.synthetic_band_trial(ceil * 1.4, ppm, es_n0_db=15.0, seed=3)
    assert below["in_range"] and below["acquired"] and below["ber"] < 1e-2
    assert (not above["in_range"]) and (not above["acquired"])  # CFO out of range -> no correct lock


def test_ceiling_moves_with_ppm():
    # a better-disciplined LO (lower ppm) pushes the ceiling higher
    Rs = band.symbol_rate_hz()
    assert band.band_ceiling_hz(10, Rs) > band.band_ceiling_hz(25, Rs) > band.band_ceiling_hz(40, Rs)


def test_low_ppm_acquires_at_a_high_band_where_high_ppm_fails():
    fc = 3.0e9
    good = band.synthetic_band_trial(fc, 5.0, es_n0_db=15.0, seed=1)    # 5 ppm -> ceiling ~7.8 GHz
    bad = band.synthetic_band_trial(fc, 40.0, es_n0_db=15.0, seed=1)    # 40 ppm -> ceiling ~1.0 GHz
    assert good["acquired"] and good["ber"] < 1e-2
    assert not bad["acquired"]


def test_sweep_reports_a_clean_knee():
    Rs = band.symbol_rate_hz()
    ceil = band.band_ceiling_hz(25.0, Rs)
    bands = [ceil * 0.5, ceil * 0.8, ceil * 1.2, ceil * 1.6]
    rows = band.band_sweep(bands, 25.0, trials=8, es_n0_db=15.0)
    rates = [r["acq_rate"] for r in rows]
    assert rates[0] >= 0.9 and rates[1] >= 0.9      # locks below the ceiling
    assert rates[-1] <= 0.1                          # fails well above it
    assert np.all(np.diff(rates) <= 1e-9)            # monotonic non-increasing across the knee
