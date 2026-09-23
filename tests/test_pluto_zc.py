"""PlutoBackend Zadoff-Chu decode path, end-to-end against a synthetic channel (needs GNU Radio).

No radios: mirrors run_link's exact TX frame construction (circular-conv RRC pulse shaping),
pushes it through a tiled-periods + CFO + AWGN channel, and exercises the real _decode_zc. Guards
the wiring (matched filter, normalization, DD-PLL residual, grading alignment) that the pure-numpy
sync tests don't cover.
"""
import numpy as np
import pytest

from gr_autopilot import modulation
from gr_autopilot.link import sync

gr = pytest.importorskip("gnuradio", reason="GNU Radio not installed")
from gnuradio.filter import firdes            # noqa: E402
from gr_autopilot.link.pluto import PlutoBackend  # noqa: E402

pytestmark = pytest.mark.gnuradio


def _synth_capture(pre, tx_syms, sps, rolloff, tx_scale, cfo_cyc_sym, noise_sigma, periods, rng):
    frame = np.concatenate([pre, tx_syms])
    up = np.zeros(frame.size * sps, dtype=complex)
    up[::sps] = frame
    ntaps = 8 * sps + 1
    rrc = np.asarray(firdes.root_raised_cosine(1.0, float(sps), 1.0, rolloff, ntaps))
    wave = np.fft.ifft(np.fft.fft(up) * np.fft.fft(rrc, up.size))   # circular conv (periodic)
    wave = wave / np.max(np.abs(wave)) * tx_scale
    cap = np.tile(wave, periods)
    n = np.arange(cap.size)
    cap = cap * np.exp(1j * 2 * np.pi * (cfo_cyc_sym / sps) * n)
    cap = cap + noise_sigma * (rng.standard_normal(cap.size) + 1j * rng.standard_normal(cap.size))
    return cap - cap.mean()


def _decode(mod, cfo, sigma, n_syms=1000, sps=8, rolloff=0.35):
    be = PlutoBackend("x", "y", sync_mode="zc")
    const = modulation.get(mod)
    bps = const.bits_per_symbol
    tx_bits = np.random.default_rng(1).integers(0, 2, n_syms * bps).astype(np.int8)
    tx_syms = const.modulate(tx_bits)
    pre = sync.zc_preamble(be.zc_half, be.zc_root)
    cap = _synth_capture(pre, tx_syms, sps, rolloff, be.tx_scale, cfo, sigma, 4,
                         np.random.default_rng(7))
    _, _, txb_al, _, rx_bits, dbg = be._decode_zc(cap, const, tx_bits, tx_syms, pre, sps, rolloff)
    ber = float(np.mean(rx_bits != txb_al))
    return ber, dbg


@pytest.mark.parametrize("mod", ["bpsk", "qpsk"])
@pytest.mark.parametrize("cfo", [0.0, 0.05, 0.10])
def test_zc_decode_ber_zero_bpsk_qpsk(mod, cfo):
    """BPSK/QPSK decode error-free through the ZC path across a wide CFO, at moderate noise."""
    ber, dbg = _decode(mod, cfo, sigma=0.10)
    assert dbg["zc_locked"] is True
    assert dbg["cfo_cyc_per_sym"] == pytest.approx(cfo, abs=3e-3)  # CFO recovered
    assert ber == 0.0


def test_zc_decode_16qam_clean():
    """16-QAM (modulation-independent acquisition) decodes at low noise -- ZC handles it too."""
    ber, dbg = _decode("16qam", cfo=0.08, sigma=0.03)
    assert dbg["zc_locked"] is True
    assert ber < 1e-2


def test_zc_no_lock_is_honest():
    """Pure noise -> no preamble -> honest no-lock (not a fake success)."""
    be = PlutoBackend("x", "y", sync_mode="zc")
    const = modulation.get("qpsk")
    tx_bits = np.random.default_rng(1).integers(0, 2, 2000).astype(np.int8)
    tx_syms = const.modulate(tx_bits)
    pre = sync.zc_preamble(be.zc_half, be.zc_root)
    noise = (np.random.default_rng(0).standard_normal(60000)
             + 1j * np.random.default_rng(2).standard_normal(60000))
    _, _, _, _, _, dbg = be._decode_zc(noise, const, tx_bits, tx_syms, pre, 8, 0.35)
    assert dbg["zc_locked"] is False
