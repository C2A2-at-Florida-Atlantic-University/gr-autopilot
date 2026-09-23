"""Co-channel interference physics: spectral overlap and the SNR it costs the link."""
import numpy as np
import pytest

from gr_autopilot.link.interference import Interferer

LINK_BW = 350_000.0  # ~symbol_rate*(1+rolloff) at the bench settings


def test_cw_on_channel_full_overlap():
    itf = Interferer(center_freq_hz=2.4001e9, inr_db=20.0, kind="cw")
    assert itf.in_band_fraction(2.4e9, LINK_BW) == 1.0  # 100 kHz off, inside +-175 kHz


def test_cw_off_channel_no_overlap():
    itf = Interferer(center_freq_hz=2.4001e9, inr_db=20.0, kind="cw")
    assert itf.in_band_fraction(2.41e9, LINK_BW) == 0.0  # link moved 10 MHz away


def test_cw_overlap_is_a_step_at_half_bandwidth():
    itf = Interferer(center_freq_hz=2.4e9, kind="cw")
    assert itf.in_band_fraction(2.4e9 + 174_000, LINK_BW) == 1.0   # just inside
    assert itf.in_band_fraction(2.4e9 + 176_000, LINK_BW) == 0.0   # just outside


def test_noise_partial_overlap():
    itf = Interferer(center_freq_hz=2.4e9, kind="noise", bandwidth_hz=200_000.0, inr_db=20.0)
    # link shifted +150 kHz: interferer [-100,+100] kHz vs link [-25,+325] kHz -> 125/200 overlap
    frac = itf.in_band_fraction(2.4e9 + 150_000, LINK_BW)
    assert frac == pytest.approx(0.625)


def test_full_overlap_collapses_snr_by_inr():
    itf = Interferer(center_freq_hz=2.4e9, inr_db=20.0, kind="cw")
    # clean 20 dB link, 20 dB-INR interferer dead on channel -> ~0 dB effective (SIR ~ 0)
    eff = itf.effective_es_n0_db(2.4e9, LINK_BW, clean_es_n0_db=20.0)
    assert eff == pytest.approx(0.0, abs=0.1)


def test_avoidance_restores_clean_snr():
    itf = Interferer(center_freq_hz=2.4e9, inr_db=30.0, kind="cw")
    jammed = itf.effective_es_n0_db(2.4e9, LINK_BW, clean_es_n0_db=18.0)
    avoided = itf.effective_es_n0_db(2.42e9, LINK_BW, clean_es_n0_db=18.0)
    assert jammed < 0.0            # a 30 dB-INR tone buries an 18 dB link
    assert avoided == pytest.approx(18.0)  # move 20 MHz away -> fully recovered
