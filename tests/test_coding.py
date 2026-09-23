"""Convolutional code + Viterbi tests: exact round-trip, error correction, coding gain."""
import numpy as np

from gr_autopilot.coding import (
    CODE_RATE,
    coding_decode,
    coding_encode,
    conv_encode,
    viterbi_decode,
)


def test_round_trip_exact():
    rng = np.random.default_rng(0)
    bits = rng.integers(0, 2, 1000)
    assert np.array_equal(viterbi_decode(conv_encode(bits), bits.size), bits)


def test_rate_half_with_tail_flush():
    coded = conv_encode(np.zeros(100, dtype=int))
    assert coded.size == (100 + 2) * 2          # 2-bit tail flush, rate 1/2
    assert abs(CODE_RATE - 0.5) < 1e-9


def test_corrects_sparse_errors():
    rng = np.random.default_rng(2)
    bits = rng.integers(0, 2, 500)
    coded = conv_encode(bits)
    c = coded.copy()
    c[rng.choice(coded.size, 5, replace=False)] ^= 1   # 5 isolated coded-bit flips
    assert np.array_equal(viterbi_decode(c, bits.size), bits)


def test_rate34_puncture_round_trip():
    rng = np.random.default_rng(4)
    bits = rng.integers(0, 2, 600)
    coded = coding_encode(bits, "conv_k3_r34")
    assert abs(bits.size / coded.size - 0.75) < 0.02              # punctured to rate 3/4
    assert np.array_equal(coding_decode(coded, "conv_k3_r34", bits.size), bits)   # erasure Viterbi
    # and the rate-1/2 path via the same helpers
    c12 = coding_encode(bits, "conv_k3_r12")
    assert np.array_equal(coding_decode(c12, "conv_k3_r12", bits.size), bits)


def test_coding_gain_at_moderate_snr():
    # At Eb/N0 = 6 dB the coded link has a lower info-BER than uncoded BPSK (hard-decision gain);
    # coded symbols carry Es = rate*Eb, so the comparison is fair (same energy per info bit).
    rng = np.random.default_rng(3)
    n = 20000
    ebn0 = 10 ** (6 / 10.0)
    sigma = np.sqrt(1.0 / (2 * ebn0))
    bits = rng.integers(0, 2, n)
    ber_u = float(np.mean(bits != ((1 - 2 * bits) + sigma * rng.standard_normal(n) < 0)))
    coded = conv_encode(bits)
    rxc = (1 - 2 * coded) * np.sqrt(CODE_RATE) + sigma * rng.standard_normal(coded.size)
    ber_c = float(np.mean(bits != viterbi_decode((rxc < 0).astype(int), n)))
    assert ber_c < ber_u
