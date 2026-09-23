"""The constellation module is the single source of truth for every modulation the framework
can transmit, decide and score. Three properties every entry must have: unit average energy (so
Es/N0 means one thing across rungs), Gray mapping (nearest neighbours differ in one bit, which
is what the closed-form BER approximations assume), and an exact modulate -> decide round trip.
"""
import numpy as np
import pytest

from gr_autopilot import modulation as M

LADDER = ("bpsk", "qpsk", "8psk", "16qam", "32qam", "64qam", "256qam")
BPS = {"bpsk": 1, "qpsk": 2, "8psk": 3, "16qam": 4, "32qam": 5, "64qam": 6, "256qam": 8}


def test_ladder_is_ascending_bits_per_symbol():
    assert M.MODULATIONS == LADDER
    assert [M.bits_per_symbol(m) for m in LADDER] == sorted(BPS[m] for m in LADDER)


@pytest.mark.parametrize("mod", LADDER)
def test_unit_energy_and_order(mod):
    c = M.get(mod)
    assert c.order == 1 << BPS[mod]
    assert np.mean(np.abs(c.points) ** 2) == pytest.approx(1.0, abs=1e-9)


def _nearest_pairs(points):
    d = np.abs(points[:, None] - points[None, :])
    dmin = d[d > 1e-9].min()
    return [(i, j) for i in range(len(points)) for j in range(i + 1, len(points))
            if abs(d[i, j] - dmin) < 1e-6]


@pytest.mark.parametrize("mod", ["bpsk", "qpsk", "8psk", "16qam", "64qam", "256qam"])
def test_gray_mapping_square_and_psk(mod):
    pts = M.get(mod).points
    for i, j in _nearest_pairs(pts):
        assert bin(i ^ j).count("1") == 1, f"{mod}: points {i},{j} differ by more than one bit"


def test_32qam_cross_is_gray_where_a_cross_can_be():
    # A cross constellation cannot be perfectly Gray; the in-grid neighbours are, and the count
    # of two-bit neighbours is the small number the layout forces (across the removed corners).
    pts = M.get("32qam").points
    pairs = _nearest_pairs(pts)
    bad = [p for p in pairs if bin(p[0] ^ p[1]).count("1") != 1]
    assert len(pairs) == 52 and len(bad) <= 8


@pytest.mark.parametrize("mod", LADDER)
def test_modulate_then_decide_is_exact(mod):
    c = M.get(mod)
    rng = np.random.default_rng(3)
    bits = rng.integers(0, 2, c.bits_per_symbol * 4000)
    rx, ref = c.hard_decision(c.modulate(bits))
    assert (rx == bits).all()
    assert np.allclose(ref, c.modulate(bits))


def test_aliases_and_min_distance():
    assert M.canonical("qam64") == "64qam" and M.canonical("PSK8") == "8psk"
    assert M.get("qam16") is M.get("16qam")
    dm = [M.min_distance(m) for m in LADDER]
    assert dm == sorted(dm, reverse=True)           # every rung is harder than the last
    assert M.min_distance("16qam") == pytest.approx(2 / np.sqrt(10), rel=1e-6)
    with pytest.raises(ValueError):
        M.get("128qam")
