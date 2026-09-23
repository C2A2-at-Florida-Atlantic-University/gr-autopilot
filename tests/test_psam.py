"""Pilot-symbol-aided modulation (PSAM) — rotation-invariant carrier recovery for 16-QAM.

The bug it fixes, first seen on the bench: the decision-directed carrier loop can settle on
a 90°-rotated alias of the rotationally-symmetric 16-QAM constellation. Harmless for an uncoded link
(reads as a bad-BER link) but fatal for a coded one — a constant rotation permutes every symbol's
bits, so Viterbi decodes garbage. PSAM measures the absolute phase at known pilots and interpolates,
so no rotation (constant or a mid-payload slip) survives. No radios / no GNU Radio here.
"""
from __future__ import annotations

import numpy as np

from gr_autopilot import modulation
from gr_autopilot.coding import coding_decode, coding_encode, info_bits_for_budget
from gr_autopilot.link.sync import (
    PILOT_SYM, dd_carrier, psam_derotate, psam_insert, psam_layout, psam_slip_correct)
from gr_autopilot.scoring.payload import known_payload

_M16 = modulation.get("16qam")


def _ber(a, b):
    n = min(len(a), len(b))
    return float(np.mean(np.asarray(a[:n]) != np.asarray(b[:n])))


def _awgn(rng, n, es_n0_db):
    n0 = 10.0 ** (-es_n0_db / 10.0)
    return np.sqrt(n0 / 2.0) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def test_psam_layout_roundtrip():
    total, is_pilot = psam_layout(1000, spacing=16)
    assert is_pilot.sum() == (1000 + 15) // 16          # one pilot per block
    assert total == 1000 + is_pilot.sum()
    syms = _M16.modulate(known_payload(4000, seed=1))
    frame, mask = psam_insert(syms, spacing=16)
    assert np.array_equal(frame[~mask], syms)           # data recoverable by the mask
    assert np.allclose(frame[mask], PILOT_SYM)


def test_psam_recovers_a_constant_90_degree_rotation():
    rng = np.random.default_rng(0)
    bits = known_payload(4000, seed=1)
    frame, is_pilot = psam_insert(_M16.modulate(bits), spacing=16)
    rx = frame * np.exp(1j * np.pi / 2) + _awgn(rng, frame.size, 20.0)   # the 90° alias + noise
    fixed, _ = _M16.hard_decision(psam_derotate(rx, is_pilot))
    assert _ber(bits, fixed) < 1e-2                                      # rotation removed
    raw, _ = _M16.hard_decision(rx[~is_pilot])                          # no correction
    assert _ber(bits, raw) > 0.2                                        # 90° permutes the bits


def test_psam_recovers_a_midpayload_slip():
    rng = np.random.default_rng(1)
    bits = known_payload(4000, seed=2)
    frame, is_pilot = psam_insert(_M16.modulate(bits), spacing=16)
    ph = np.zeros(frame.size)
    ph[frame.size // 2:] = np.pi / 2                                     # 90° slip halfway through
    rx = frame * np.exp(1j * ph) + _awgn(rng, frame.size, 20.0)
    fixed, _ = _M16.hard_decision(psam_derotate(rx, is_pilot))
    assert _ber(bits, fixed) < 1e-2


def test_psam_makes_coded_16qam_survive_the_slip_where_dd_fails():
    rng = np.random.default_rng(3)
    coding = "conv_k3_r12"
    n_info = info_bits_for_budget(4000, coding)
    info = known_payload(n_info, seed=5)
    coded = coding_encode(info, coding)
    cbits = np.concatenate([coded, np.zeros((-coded.size) % 4, dtype=coded.dtype)])
    frame, is_pilot = psam_insert(_M16.modulate(cbits), spacing=16)
    ph = np.zeros(frame.size)
    ph[frame.size // 2:] = np.pi / 2                                     # the mid-payload slip
    rx = frame * np.exp(1j * ph) + _awgn(rng, frame.size, 16.0)

    # PSAM -> Viterbi: recovers the info bits through the slip
    psam_cbits, _ = _M16.hard_decision(psam_derotate(rx, is_pilot))
    psam_info = coding_decode(np.asarray(psam_cbits)[:coded.size], coding, n_info)
    assert _ber(info, psam_info) < 1e-2

    # the decision-directed loop settles on the rotated alias -> coded decode is far worse
    dd_cbits, _ = _M16.hard_decision(dd_carrier(rx[~is_pilot], _M16.points))
    dd_info = coding_decode(np.asarray(dd_cbits)[:coded.size], coding, n_info)
    assert _ber(info, dd_info) > 10 * max(_ber(info, psam_info), 1e-4)


def test_slip_correct_fixes_a_constant_alias_the_hardware_case():
    # the real hardware failure is the DD loop locking to a CONSTANT 90° alias, not a
    # mid-payload jump. DD fine-tracks; the pilots resolve the constant rotation (rounding to 90°,
    # noise-robust). Pilot is a corner point so DD is not perturbed by it.
    rng = np.random.default_rng(11)
    coding = "conv_k3_r12"
    n_info = info_bits_for_budget(4000, coding)
    info = known_payload(n_info, seed=13)
    coded = coding_encode(info, coding)
    cbits = np.concatenate([coded, np.zeros((-coded.size) % 4, dtype=coded.dtype)])
    corner = _M16.points[int(np.argmax(np.abs(_M16.points)))]
    frame, is_pilot = psam_insert(_M16.modulate(cbits), spacing=16, pilot=corner)
    rx = frame * np.exp(1j * np.pi / 2) + _awgn(rng, frame.size, 15.0)   # locked to the 90° alias

    dd = dd_carrier(rx, _M16.points)                          # fine tracking (pilots don't perturb it)
    data = psam_slip_correct(dd, is_pilot, corner)            # discrete 90° removal
    rx_cbits, _ = _M16.hard_decision(data)
    rx_info = coding_decode(np.asarray(rx_cbits)[:coded.size], coding, n_info)
    assert _ber(info, rx_info) < 1e-2                         # recovered through the constant alias
