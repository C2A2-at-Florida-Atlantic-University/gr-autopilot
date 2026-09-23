"""Scoring-layer unit tests (no hardware, no GNU Radio).

Validates BER / EVM / SNR against inputs with *known* answers, and the PRBS payload's
determinism and balance. These prove the framework-owned grader is correct in isolation,
independent of any radio.
"""
import math

import numpy as np
import pytest

from gr_autopilot import modulation
from gr_autopilot.scoring import metrics, payload


# ---- BER --------------------------------------------------------------------

def test_ber_counts_known_errors():
    tx = np.array([0, 1, 0, 1, 1, 0, 0, 1], dtype=np.int8)
    rx = tx.copy()
    rx[2] ^= 1
    rx[5] ^= 1  # exactly 2 flipped bits
    r = metrics.ber(tx, rx)
    assert r.n_bits == 8
    assert r.n_errors == 2
    assert r.ber == pytest.approx(2 / 8)


def test_ber_zero_when_identical():
    tx = payload.known_payload(1000)
    assert metrics.ber(tx, tx.copy()).ber == 0.0


def test_ber_all_wrong():
    tx = np.zeros(100, dtype=np.int8)
    rx = np.ones(100, dtype=np.int8)
    assert metrics.ber(tx, rx).ber == 1.0


def test_ber_raises_on_length_mismatch():
    with pytest.raises(ValueError):
        metrics.bit_errors(np.zeros(10), np.zeros(11))


# ---- EVM / SNR --------------------------------------------------------------

def test_evm_zero_on_perfect_symbols():
    const = modulation.get("qpsk")
    bits = payload.known_payload(4000)
    syms = const.modulate(bits)
    e = metrics.evm(syms, syms)
    assert e["evm_pct"] == pytest.approx(0.0, abs=1e-9)


def test_evm_matches_known_offset():
    # All symbols at (1+0j); push each by a fixed real error 0.1 => EVM = 0.1 / 1.0.
    ref = np.ones(500, dtype=np.complex128)
    rx = ref + 0.1
    e = metrics.evm(rx, ref)
    assert e["evm_rms"] == pytest.approx(0.1, rel=1e-9)
    assert e["evm_pct"] == pytest.approx(10.0, rel=1e-9)


def test_data_aided_snr_matches_injected_noise():
    # Unit-power reference + complex noise of variance sigma^2 => SNR ~ 1/sigma^2.
    rng = np.random.default_rng(0)
    n = 200_000
    const = modulation.get("qpsk")
    bits = payload.known_payload(n * 2)
    ref = const.modulate(bits)  # unit average energy
    sigma2 = 0.05
    noise = math.sqrt(sigma2 / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    rx = ref + noise
    snr_db = metrics.data_aided_snr_db(rx, ref)
    expected = 10 * math.log10(1.0 / sigma2)
    assert snr_db == pytest.approx(expected, abs=0.15)


def test_snr_from_evm_consistency():
    # SNR-from-EVM and data-aided SNR must agree (they are the same quantity).
    rng = np.random.default_rng(1)
    n = 100_000
    ref = np.ones(n, dtype=np.complex128)
    rx = ref + 0.1 * (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / math.sqrt(2)
    e = metrics.evm(rx, ref)
    assert metrics.snr_from_evm_db(e["evm_rms"]) == pytest.approx(
        metrics.data_aided_snr_db(rx, ref), abs=0.05
    )


# ---- Payload (PRBS) ---------------------------------------------------------

def test_prbs_deterministic():
    a = payload.prbs(order=15, n_bits=5000, seed=1)
    b = payload.prbs(order=15, n_bits=5000, seed=1)
    assert np.array_equal(a, b)


def test_prbs_seed_changes_sequence():
    a = payload.prbs(order=15, n_bits=5000, seed=1)
    b = payload.prbs(order=15, n_bits=5000, seed=2)
    assert not np.array_equal(a, b)


def test_prbs_is_balanced_and_binary():
    bits = payload.prbs(order=15, n_bits=20000, seed=1)
    assert set(np.unique(bits)).issubset({0, 1})
    frac_ones = bits.mean()
    assert 0.45 < frac_ones < 0.55  # a maximal-length sequence is nearly balanced


def test_prbs_period_is_maximal():
    order = 7  # short order so the whole period fits
    period = 2**order - 1
    bits = payload.prbs(order=order, n_bits=2 * period, seed=1)
    # The sequence repeats with the maximal period 2**order - 1.
    assert np.array_equal(bits[:period], bits[period : 2 * period])


def test_prbs_rejects_bad_order():
    with pytest.raises(ValueError):
        payload.prbs(order=8, n_bits=10)
