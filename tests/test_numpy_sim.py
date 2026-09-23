"""NumpySimBackend tests: BER must track closed-form AWGN theory (no hardware).

This is the load-bearing correctness check for the whole slice: if the end-to-end
modulate -> AWGN -> hard-decision -> BER path matches theory across modulations and
Es/N0, then the modulation mapping and the scoring layer are jointly trusted.
"""
import math

import pytest
from scipy.special import erfc

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.scoring.metrics import compute_metrics

BPS = {"bpsk": 1, "qpsk": 2, "8psk": 3, "16qam": 4, "32qam": 5, "64qam": 6, "256qam": 8}


def qfunc(x):
    return 0.5 * erfc(x / math.sqrt(2.0))


def theory_ber(mod, eb_n0_db):
    ebn0 = 10.0 ** (eb_n0_db / 10.0)
    if mod in ("bpsk", "qpsk"):
        return qfunc(math.sqrt(2.0 * ebn0))
    if mod == "8psk":
        # Gray M-PSK, nearest-neighbour approximation: Pb ~ (2/k) Q( sqrt(2 k Eb/N0) sin(pi/M) )
        k, m = 3, 8
        return (2.0 / k) * qfunc(math.sqrt(2.0 * k * ebn0) * math.sin(math.pi / m))
    if mod in ("16qam", "64qam", "256qam"):
        # Gray square M-QAM, nearest-neighbour approximation:
        # Pb ~ (4/k) (1 - 1/sqrt(M)) Q( sqrt(3 k Eb/N0 / (M - 1)) )   (k=4 gives the 0.75 Q(sqrt(0.8 Eb/N0)) form)
        k = BPS[mod]
        m = 1 << k
        return (4.0 / k) * (1.0 - 1.0 / math.sqrt(m)) * qfunc(math.sqrt(3.0 * k * ebn0 / (m - 1)))
    raise ValueError(mod)


def measure_ber(mod, eb_n0_db, n_bits):
    es_n0 = eb_n0_db + 10.0 * math.log10(BPS[mod])
    r = NumpySimBackend().run_link(
        LinkParams(modulation=mod, n_payload_bits=n_bits, es_n0_db=es_n0, seed=1)
    )
    return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)


@pytest.mark.parametrize("mod", ["bpsk", "qpsk"])
@pytest.mark.parametrize("eb_n0_db", [2.0, 4.0, 6.0])
def test_bpsk_qpsk_ber_matches_theory(mod, eb_n0_db):
    # ~2M bits => >~4000 errors even at 6 dB, enough for a tight relative tolerance.
    m = measure_ber(mod, eb_n0_db, n_bits=2_000_000)
    assert m.ber == pytest.approx(theory_ber(mod, eb_n0_db), rel=0.10)


@pytest.mark.parametrize("eb_n0_db", [6.0, 8.0])
def test_16qam_ber_matches_theory_approx(eb_n0_db):
    m = measure_ber("16qam", eb_n0_db, n_bits=4_000_000)
    # 16-QAM BER formula is an approximation; allow a looser tolerance.
    assert m.ber == pytest.approx(theory_ber("16qam", eb_n0_db), rel=0.20)


@pytest.mark.parametrize("eb_n0_db", [6.0, 8.0])
def test_8psk_ber_matches_theory_approx(eb_n0_db):
    m = measure_ber("8psk", eb_n0_db, n_bits=3_000_000)
    assert m.ber == pytest.approx(theory_ber("8psk", eb_n0_db), rel=0.15)


@pytest.mark.parametrize("mod,eb_n0_db", [("64qam", 10.0), ("64qam", 12.0), ("256qam", 14.0), ("256qam", 16.0)])
def test_high_order_qam_ber_matches_theory_approx(mod, eb_n0_db):
    # Same Gray-square approximation as 16-QAM; a little looser because the neighbour count
    # varies more across the grid at high order.
    m = measure_ber(mod, eb_n0_db, n_bits=4_000_000)
    assert m.ber == pytest.approx(theory_ber(mod, eb_n0_db), rel=0.25)


def test_32qam_sits_between_16_and_64():
    # No tidy closed form for the cross, but it must cost more Eb/N0 than 16-QAM and less than 64.
    at = lambda mod: measure_ber(mod, 10.0, n_bits=1_000_000).ber
    assert at("16qam") < at("32qam") < at("64qam")


@pytest.mark.parametrize("mod", list(BPS))
def test_ber_monotonic_in_snr(mod):
    # A 7.5 dB sweep starting ~1.5 dB higher per extra bit keeps every point inside the region
    # where 800k bits still count errors, so the strict inequality compares measurements, not
    # a run of zeros at the top.
    lo = 1.0 + max(0, BPS[mod] - 2) * 1.5
    bers = [measure_ber(mod, e, n_bits=800_000).ber for e in (lo, lo + 2.5, lo + 5.0, lo + 7.5)]
    assert bers[-1] > 0, f"{mod}: top of the sweep has no errors to compare ({bers})"
    assert all(bers[i] > bers[i + 1] for i in range(len(bers) - 1)), bers


@pytest.mark.parametrize("mod", list(BPS))
def test_clean_link_is_error_free(mod):
    # A very clean link should produce zero bit errors -- 256-QAM needs ~10 dB more than 16-QAM.
    m = measure_ber(mod, eb_n0_db=25.0 + (10.0 if mod == "256qam" else 0.0), n_bits=200_000)
    assert m.ber == 0.0


def test_measured_snr_tracks_es_n0():
    # Data-aided SNR (per-symbol Es/N0) should track the configured Es/N0 within ~1 dB.
    es_n0 = 12.0
    r = NumpySimBackend().run_link(
        LinkParams(modulation="qpsk", n_payload_bits=1_000_000, es_n0_db=es_n0, seed=3)
    )
    m = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
    assert m.snr_db == pytest.approx(es_n0, abs=1.0)
