"""Soft-decision Viterbi — ~2 dB coding gain from demodulator LLRs (no radios).

Checks the soft demod (LLR sign agrees with the hard decision; magnitude carries reliability), the
soft-input Viterbi (matches hard at high confidence), and that soft beats hard at a marginal SNR for
both the rate-1/2 and the punctured rate-3/4 code.
"""
from __future__ import annotations

import numpy as np

from gr_autopilot import modulation
from gr_autopilot.coding import (coding_decode, coding_decode_soft, coding_encode,
                                 info_bits_for_budget)
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.scoring.metrics import compute_metrics
from gr_autopilot.scoring.payload import known_payload

_BE = NumpySimBackend()


def _coded_ber(es, soft, coding="conv_k3_r12", mod="qpsk", bits=60_000):
    r = _BE.run_link(LinkParams(modulation=mod, coding=coding, n_payload_bits=bits, es_n0_db=es,
                                soft_decision=soft, seed=3))
    return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms).ber


def test_soft_bits_sign_agrees_with_the_hard_decision():
    const = modulation.get("16qam")
    syms = const.modulate(known_payload(4000, seed=1)) + 0.05 * (np.random.default_rng(0).standard_normal(1000)
                                                                 + 1j * np.random.default_rng(1).standard_normal(1000))
    hard, _ = const.hard_decision(syms)
    llr = const.soft_bits(syms)
    assert np.array_equal((llr < 0).astype(np.int8), hard.astype(np.int8))   # LLR<0 -> bit 1


def test_soft_viterbi_matches_hard_when_confident():
    # strong, clean LLRs (sign = the true coded bits, big magnitude) -> same codeword as hard Viterbi
    info = known_payload(200, seed=2)
    coded = coding_encode(info, "conv_k3_r12")
    llr = (1 - 2 * coded.astype(np.float64)) * 8.0                # +8 for bit0, -8 for bit1
    assert np.array_equal(coding_decode_soft(llr, "conv_k3_r12", info.size), info)
    assert np.array_equal(coding_decode(coded, "conv_k3_r12", info.size), info)


def test_soft_beats_hard_at_marginal_snr_rate_half():
    assert _coded_ber(3.0, soft=True) < _coded_ber(3.0, soft=False) / 3.0    # clearly better, not noise


def test_soft_beats_hard_for_the_punctured_rate_three_quarter():
    assert _coded_ber(6.0, soft=True, coding="conv_k3_r34") < _coded_ber(6.0, soft=False, coding="conv_k3_r34")


def test_soft_decode_is_error_free_at_good_snr():
    assert _coded_ber(9.0, soft=True) == 0.0


def test_soft_decision_flag_defaults_off_and_is_opt_in():
    # the default coded path is unchanged (hard), so existing results are stable
    hard_default = _coded_ber(4.0, soft=False)
    assert LinkParams().soft_decision is False
    assert _coded_ber(4.0, soft=False) == hard_default
