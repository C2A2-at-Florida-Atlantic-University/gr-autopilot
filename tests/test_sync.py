"""Zadoff-Chu acquisition: correctness and the low-SNR acquisition floor.

Builds a synthetic pulse-shaped channel (fractional delay + CFO + AWGN) in pure numpy -- no
GNU Radio -- so it can exercise exactly what the blind Gardner/Costas front-end could not:
data-aided acquisition well below the tracking-loop lock floor.
"""
from __future__ import annotations

import numpy as np
import pytest

from gr_autopilot import modulation
from gr_autopilot.link import sync


# -- helpers: a synthetic TX -> channel -> matched-filter chain --

def _tx_waveform(payload_syms, sps, rolloff, half_len, root):
    pre = sync.zc_preamble(half_len, root)
    frame = np.concatenate([pre, payload_syms])
    up = np.zeros(frame.size * sps, dtype=complex)
    up[::sps] = frame
    h = sync.rrc_taps(sps, rolloff)
    return np.convolve(up, h), pre.size


def _frac_delay(x, d):
    """Sub-sample delay by ``d`` samples via FFT phase ramp (exact for band-limited x)."""
    N = x.size
    f = np.fft.fftfreq(N)
    return np.fft.ifft(np.fft.fft(x) * np.exp(-1j * 2 * np.pi * f * d))


def _channel(wave, sps, cfo_sym, delay, noise_sigma, rng, pad=37):
    """Apply integer+fractional delay, CFO (cycles/symbol), and complex AWGN."""
    wave = _frac_delay(wave, delay - int(delay))
    wave = np.concatenate([np.zeros(pad + int(delay), dtype=complex), wave,
                           np.zeros(200, dtype=complex)])
    n = np.arange(wave.size)
    wave = wave * np.exp(1j * 2 * np.pi * (cfo_sym / sps) * n)          # CFO at sample level
    noise = noise_sigma * (rng.standard_normal(wave.size) + 1j * rng.standard_normal(wave.size))
    return wave + noise


def _receive(rx, sps, rolloff):
    mf = np.convolve(rx, sync.rrc_taps(sps, rolloff))
    return mf - mf.mean()


# -- Zadoff-Chu sequence properties --

def test_zc_constant_modulus():
    z = sync.zadoff_chu(64, 25)
    assert np.allclose(np.abs(z), 1.0)


def test_zc_perfect_periodic_autocorrelation():
    z = sync.zadoff_chu(64, 25)
    # circular autocorrelation: zero at every nonzero lag
    ac = np.array([np.abs(np.vdot(z, np.roll(z, k))) for k in range(64)])
    assert ac[0] == pytest.approx(64.0)
    assert np.max(ac[1:]) < 1e-9


def test_zc_root_must_be_coprime():
    with pytest.raises(ValueError):
        sync.zadoff_chu(64, 8)  # gcd(8,64)=8


def test_schmidl_cox_peaks_at_preamble_start():
    sps, half = 8, 64
    payload = modulation.get("qpsk").modulate(np.random.default_rng(0).integers(0, 2, 400))
    wave, _ = _tx_waveform(payload, sps, 0.35, half, 25)
    pad = 50
    rx = np.concatenate([np.zeros(pad, dtype=complex), wave])
    mf = _receive(rx, sps, 0.35)
    M, _ = sync.schmidl_cox_metric(mf, half * sps)
    # peak within a couple samples of the (filter-delayed) preamble start
    peak = int(np.argmax(M))
    filt_delay = len(sync.rrc_taps(sps, 0.35)) - 1  # two RRC convolutions -> group delay
    assert abs(peak - (pad + filt_delay)) <= sps


# -- end-to-end acquisition --

def test_acquire_noiseless_exact():
    sps, half, rolloff = 8, 64, 0.35
    rng = np.random.default_rng(1)
    bits = rng.integers(0, 2, 800)
    payload = modulation.get("qpsk").modulate(bits)
    wave, npre = _tx_waveform(payload, sps, rolloff, half, 25)
    rx = _channel(wave, sps, cfo_sym=0.02, delay=5.3, noise_sigma=0.0, rng=rng)
    mf = _receive(rx, sps, rolloff)

    acq = sync.acquire(mf, sps, half, 25)
    assert acq is not None
    assert acq["cfo"] == pytest.approx(0.02, abs=1e-3)     # recovered CFO (cycles/symbol)
    syms = sync.extract_symbols(mf, acq, npre + payload.size)
    pay = sync.dd_carrier(syms[npre:], modulation.get("qpsk").points)
    rx_bits, _ = modulation.get("qpsk").hard_decision(pay[:payload.size])
    assert np.array_equal(rx_bits, bits)                   # zero BER, absolute phase pinned


def test_acquire_with_cfo_and_noise_qpsk():
    sps, half, rolloff = 8, 64, 0.35
    rng = np.random.default_rng(4)
    bits = rng.integers(0, 2, 2000)
    const = modulation.get("qpsk")
    payload = const.modulate(bits)
    wave, npre = _tx_waveform(payload, sps, rolloff, half, 25)
    # a large CFO (0.12 cyc/sym ~ the +-120 kHz worst case at this symbol rate) + noise
    rx = _channel(wave, sps, cfo_sym=0.12, delay=11.7, noise_sigma=0.18, rng=rng)
    mf = _receive(rx, sps, rolloff)

    acq = sync.acquire(mf, sps, half, 25)
    assert acq is not None
    assert acq["cfo"] == pytest.approx(0.12, abs=3e-3)
    syms = sync.extract_symbols(mf, acq, npre + payload.size)
    pay = sync.dd_carrier(syms[npre:], const.points)
    rx_bits, _ = const.hard_decision(pay[:payload.size])
    ber = np.mean(rx_bits != bits)
    assert ber < 1e-2


def _es_n0_db(sps, rolloff, half, sigma):
    """Empirical post-matched-filter symbol Es/N0 for a given sample-noise sigma."""
    payload = modulation.get("qpsk").modulate(np.random.default_rng(7).integers(0, 2, 400))
    wave, npre = _tx_waveform(payload, sps, rolloff, half, 25)
    rx = _channel(wave, sps, cfo_sym=0.0, delay=0.0, noise_sigma=0.0,
                  rng=np.random.default_rng(0))
    mf = _receive(rx, sps, rolloff)
    acq = sync.acquire(mf, sps, half, 25)
    syms = sync.extract_symbols(mf, acq, npre + payload.size)[npre:]
    p_sig = float(np.mean(np.abs(syms) ** 2))         # recovered symbol power
    p_noise = sigma ** 2                              # MF is unit-energy -> noise var passes through
    return 10.0 * np.log10(p_sig / p_noise)


def test_acquire_bpsk_phase_resolved():
    """BPSK carries through the (modulation-independent) ZC chain, and the data-aided preamble
    phase pins the 180-degree ambiguity -- so the 3rd AMC rung's modulation decodes cleanly."""
    sps, half, rolloff = 8, 64, 0.35
    rng = np.random.default_rng(9)
    bits = rng.integers(0, 2, 1500)
    const = modulation.get("bpsk")
    payload = const.modulate(bits)
    wave, npre = _tx_waveform(payload, sps, rolloff, half, 25)
    rx = _channel(wave, sps, cfo_sym=0.06, delay=9.2, noise_sigma=0.15, rng=rng)
    mf = _receive(rx, sps, rolloff)

    acq = sync.acquire(mf, sps, half, 25)
    assert acq is not None
    syms = sync.extract_symbols(mf, acq, npre + payload.size)
    pay = sync.dd_carrier(syms[npre:], const.points)
    rx_bits, _ = const.hard_decision(pay[:payload.size])
    assert np.mean(rx_bits != bits) < 1e-2  # no 180-degree slip


def test_acquisition_floor_sweep():
    """Correct-acquisition rate + payload BER vs SNR over many seeds -- the acquisition floor.

    Success is a *correct* lock (frame found AND CFO recovered), not merely the metric firing.
    The blind Gardner/Costas front-end cannot even start in this regime; ZC acquisition should
    still lock reliably at SNRs well below where the payload itself becomes usable -- and far
    below the ~11 dB hardware lock floor that blocked the 3rd AMC rung.
    """
    sps, half, rolloff = 8, 64, 0.35
    const = modulation.get("qpsk")
    trials = 40
    print("\n  Es/N0   lock%   medianBER")
    results = {}
    for sigma in (0.15, 0.3, 0.5, 0.7, 1.0):
        locked = 0
        bers = []
        for s in range(trials):
            rng = np.random.default_rng(1000 + s)
            bits = rng.integers(0, 2, 1200)
            payload = const.modulate(bits)
            wave, npre = _tx_waveform(payload, sps, rolloff, half, 25)
            rx = _channel(wave, sps, cfo_sym=0.08, delay=7.4 + 0.01 * s,
                          noise_sigma=sigma, rng=rng)
            mf = _receive(rx, sps, rolloff)
            acq = sync.acquire(mf, sps, half, 25)
            if acq is not None and abs(acq["cfo"] - 0.08) < 5e-3:
                locked += 1
                syms = sync.extract_symbols(mf, acq, npre + payload.size)
                pay = sync.dd_carrier(syms[npre:], const.points)
                rx_bits, _ = const.hard_decision(pay[:payload.size])
                bers.append(float(np.mean(rx_bits != bits)))
        rate = locked / trials
        med_ber = float(np.median(bers)) if bers else float("nan")
        esn0 = _es_n0_db(sps, rolloff, half, sigma)
        results[sigma] = rate
        print(f"  {esn0:5.1f}   {100*rate:4.0f}%   {med_ber:.2e}")
    # Correct acquisition is essentially certain at high SNR and stays >=90% down to ~6 dB Es/N0
    # -- well below the ~11 dB lock floor of the blind Gardner/Costas front-end this replaces.
    assert results[0.15] == 1.0
    assert results[0.3] >= 0.9
    assert results[0.5] >= 0.9
