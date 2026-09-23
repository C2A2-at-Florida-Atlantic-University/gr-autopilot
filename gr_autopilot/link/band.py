"""Band-dependent acquisition physics — Stage-1 sync robustness across RF bands.

The ZC acquisition front-end (``link/sync.py``) tolerates a fixed CFO *range in Hz*: ``cfo_max``
cycles/symbol times the symbol rate. But the carrier-frequency offset between two FREE-RUNNING Pluto
oscillators is ``ppm * f_c`` — it GROWS with the band. So there is a band **ceiling** above which the
ppm-scaled CFO exceeds the acquisition range and the frame no longer locks, independent of SNR. Below
it, acquisition is band-flat.

This makes "band" a first-class parameter. ``cfo_hz``/``band_ceiling_hz`` are the closed-form model;
``synthetic_band_trial`` runs the REAL ``sync.acquire``/``extract_symbols`` against a numpy
pulse-shaped channel carrying that CFO, so the operating envelope is characterized with no radios and
then confirmed on the wired Plutos with the identical acquisition code (which reports the *measured*
CFO in ``LinkResult.meta['cfo_cyc_per_sym']``). Pure numpy; no GNU Radio.
"""
from __future__ import annotations

import numpy as np

from gr_autopilot import modulation
from gr_autopilot.link import sync

# Bench front-end defaults (see scripts/run_hw_*.py / PlutoBackend).
BENCH_SAMPLE_RATE_HZ = 2_084_000.0
BENCH_SPS = 8
DEFAULT_CFO_MAX = 0.15          # sync.acquire's in-band CFO search bound, cycles/symbol


# ---- closed-form band ↔ CFO model -----------------------------------------------------------

def symbol_rate_hz(sample_rate_hz: float = BENCH_SAMPLE_RATE_HZ, sps: int = BENCH_SPS) -> float:
    return float(sample_rate_hz) / float(sps)


def cfo_hz(center_freq_hz: float, ppm: float) -> float:
    """CFO (Hz) a differential oscillator error of ``ppm`` produces at ``center_freq_hz``."""
    return float(ppm) * 1e-6 * float(center_freq_hz)


def cfo_cycles_per_symbol(cfo_hz_value: float, symbol_rate: float) -> float:
    return float(cfo_hz_value) / float(symbol_rate)


def acquisition_range_hz(symbol_rate: float, cfo_max: float = DEFAULT_CFO_MAX) -> float:
    """The largest CFO (Hz) ``sync.acquire`` can pull in: ``cfo_max`` cyc/sym × symbol rate."""
    return float(cfo_max) * float(symbol_rate)


def band_ceiling_hz(ppm: float, symbol_rate: float = None, cfo_max: float = DEFAULT_CFO_MAX) -> float:
    """Highest band whose ppm-CFO stays inside the acquisition range: ``cfo_max·Rs / (ppm·1e-6)``.
    ``inf`` for a perfectly disciplined LO (ppm→0)."""
    sr = symbol_rate if symbol_rate is not None else symbol_rate_hz()
    if ppm <= 0:
        return float("inf")
    return acquisition_range_hz(sr, cfo_max) / (ppm * 1e-6)


# ---- synthetic pulse-shaped channel (mirrors tests/test_sync.py, reusable) -------------------

def _tx_waveform(payload, sps, rolloff, half_len, root):
    pre = sync.zc_preamble(half_len, root)
    frame = np.concatenate([pre, payload])
    up = np.zeros(frame.size * sps, dtype=complex)
    up[::sps] = frame
    return np.convolve(up, sync.rrc_taps(sps, rolloff)), pre.size


def _frac_delay(x, d):
    f = np.fft.fftfreq(x.size)
    return np.fft.ifft(np.fft.fft(x) * np.exp(-1j * 2 * np.pi * f * d))


def _channel(wave, sps, cfo_sym, delay, sigma, rng, pad=37):
    wave = _frac_delay(wave, delay - int(delay))
    wave = np.concatenate([np.zeros(pad + int(delay), dtype=complex), wave,
                           np.zeros(200, dtype=complex)])
    n = np.arange(wave.size)
    wave = wave * np.exp(1j * 2 * np.pi * (cfo_sym / sps) * n)      # CFO at sample level
    return wave + sigma * (rng.standard_normal(wave.size) + 1j * rng.standard_normal(wave.size))


def _receive(rx, sps, rolloff):
    mf = np.convolve(rx, sync.rrc_taps(sps, rolloff))
    return mf - mf.mean()


_PSIG_CACHE: dict = {}


def _recovered_symbol_power(sps, rolloff, half_len, root, mod):
    """Noiseless recovered symbol power for this config — used to hit a target Es/N0."""
    key = (sps, rolloff, half_len, root, mod)
    if key not in _PSIG_CACHE:
        const = modulation.get(mod)
        payload = const.modulate(np.random.default_rng(0).integers(0, 2, 800 * const.bits_per_symbol))
        wave, npre = _tx_waveform(payload, sps, rolloff, half_len, root)
        rx = _channel(wave, sps, 0.0, 0.0, 0.0, np.random.default_rng(0))
        mf = _receive(rx, sps, rolloff)
        acq = sync.acquire(mf, sps, half_len, root)
        syms = sync.extract_symbols(mf, acq, npre + payload.size)[npre:]
        _PSIG_CACHE[key] = float(np.mean(np.abs(syms) ** 2))
    return _PSIG_CACHE[key]


def synthetic_band_trial(center_freq_hz: float, ppm: float, *, es_n0_db: float = 15.0,
                         sample_rate_hz: float = BENCH_SAMPLE_RATE_HZ, sps: int = BENCH_SPS,
                         rolloff: float = 0.35, half_len: int = 64, root: int = 25,
                         cfo_max: float = DEFAULT_CFO_MAX, n_bits: int = 2000,
                         modulation_name: str = "qpsk", lock_tol_cyc_sym: float = 5e-3,
                         seed: int = 0) -> dict:
    """One synthetic acquisition trial at ``center_freq_hz`` with a differential ``ppm`` LO error.

    Injects CFO = ppm·f_c (converted to cycles/symbol at the bench symbol rate) into a ZC-preambled,
    RRC pulse-shaped frame with a fractional delay and AWGN at ``es_n0_db``, then runs the real
    ``sync.acquire``/``extract_symbols``. ``acquired`` requires a *correct* lock (CFO recovered within
    ``lock_tol_cyc_sym``), not merely the metric firing. Returns the per-band diagnostics + payload BER.
    """
    rng = np.random.default_rng(seed)
    sr = symbol_rate_hz(sample_rate_hz, sps)
    f_cfo_hz = cfo_hz(center_freq_hz, ppm)
    cfo_sym = cfo_cycles_per_symbol(f_cfo_hz, sr)

    const = modulation.get(modulation_name)
    bits = rng.integers(0, 2, n_bits - n_bits % const.bits_per_symbol)
    payload = const.modulate(bits)
    wave, npre = _tx_waveform(payload, sps, rolloff, half_len, root)

    p_sig = _recovered_symbol_power(sps, rolloff, half_len, root, modulation_name)
    sigma = float(np.sqrt(p_sig) * 10.0 ** (-es_n0_db / 20.0))
    delay = 7.4 + (seed % 13) * 0.03
    rx = _channel(wave, sps, cfo_sym, delay, sigma, rng)
    mf = _receive(rx, sps, rolloff)

    acq = sync.acquire(mf, sps, half_len, root, cfo_max=cfo_max)
    cfo_est = float(acq["cfo"]) if acq is not None else None
    acquired = acq is not None and abs(cfo_est - cfo_sym) < lock_tol_cyc_sym

    if acquired:
        syms = sync.extract_symbols(mf, acq, npre + payload.size)
        pay = sync.dd_carrier(syms[npre:], const.points)
        rx_bits, _ = const.hard_decision(pay[:payload.size])
        ber = float(np.mean(rx_bits != bits))
    else:
        ber = 0.5   # no lock -> no usable payload

    return {
        "center_freq_hz": float(center_freq_hz), "ppm": float(ppm),
        "cfo_hz": f_cfo_hz, "cfo_cyc_sym": cfo_sym,
        "acq_range_hz": acquisition_range_hz(sr, cfo_max),
        "in_range": abs(cfo_sym) <= cfo_max,
        "acquired": bool(acquired),
        "cfo_est_cyc_sym": cfo_est,
        "cfo_err_hz": None if cfo_est is None else (cfo_est - cfo_sym) * sr,
        "ber": ber, "es_n0_db": float(es_n0_db), "symbol_rate_hz": sr,
    }


def band_sweep(bands_hz, ppm: float, *, trials: int = 12, es_n0_db: float = 15.0, **kw) -> list[dict]:
    """Acquisition rate + median BER at each band over ``trials`` seeds (the sim envelope row)."""
    out = []
    for fc in bands_hz:
        locked, bers, first = 0, [], None
        for s in range(trials):
            r = synthetic_band_trial(fc, ppm, es_n0_db=es_n0_db, seed=1000 + s, **kw)
            first = first or r
            if r["acquired"]:
                locked += 1
                bers.append(r["ber"])
        out.append({
            "center_freq_hz": float(fc), "ppm": float(ppm),
            "cfo_hz": first["cfo_hz"], "cfo_cyc_sym": first["cfo_cyc_sym"],
            "acq_range_hz": first["acq_range_hz"], "in_range": first["in_range"],
            "acq_rate": locked / trials,
            "median_ber": float(np.median(bers)) if bers else float("nan"),
        })
    return out
