"""Legacy 16-QAM frame sync vs carrier frequency offset (needs GNU Radio, no radios).

Regression guard for a bench failure seen 2026-07-28: the whole modulation ladder read healthy
(BPSK/QPSK BER 0 at ~18-21 dB SNR) while 16-QAM returned BER ~0.5 with frame-sync sharpness 0 --
a link that measures as dead at a perfectly good SNR.

Cause: 16-QAM cannot use the Costas loop (it rings a non-constant-modulus constellation), so
``_framesync_dd`` correlates a stream that is still carrier-rotated. Correlating the full
256-symbol preamble coherently makes that correlation a sinc in CFO, and it NULLS at every
integer number of cycles across the preamble. The bench pair sat at -0.0039 cyc/sym (-1020 Hz,
-0.43 ppm at 2.4 GHz) = 1.00 cycles -- exactly the first null -- so the true frame offset dropped
out of the top-K candidates and never reached the CFO-tolerant tone gate that follows.

The fix is partial-coherent candidate detection: correlate each sub-block coherently and sum
MAGNITUDES, which is phase-blind between blocks and so tolerates ~1 cycle per sub-block.

These tests pin the property that actually matters -- acquisition must not depend on where the
free-running CFO happens to land -- rather than the specific offset that bit us.
"""
import numpy as np
import pytest

from gr_autopilot import modulation
from gr_autopilot.scoring.payload import known_payload

gr = pytest.importorskip("gnuradio", reason="GNU Radio not installed")
from gnuradio.filter import firdes                        # noqa: E402
from gr_autopilot.link.pluto import PlutoBackend, sync_preamble  # noqa: E402

pytestmark = pytest.mark.gnuradio

MOD, SPS, ROLLOFF, N_SYMS = "16qam", 8, 0.35, 2048
PREAMBLE_LEN = 256
BENCH_CFO_CYC_SYM = 0.0039   # measured pluto2->pluto3 at 2.4 GHz == 1.00 cyc across the preamble


def _stream(cfo_cyc_sym, snr_db=20.0, seed=3):
    """The symbol stream _framesync_dd sees: run_link's exact TX frame, CFO + AWGN, ideal timing.

    Timing recovery is ideal on purpose -- this isolates the frame-sync/CFO interaction from the
    Gardner loop, which is covered elsewhere.
    """
    const = modulation.get(MOD)
    tx_bits = known_payload(N_SYMS * const.bits_per_symbol, order=15, seed=1)
    tx_syms = const.modulate(tx_bits)
    pre = sync_preamble(MOD, PREAMBLE_LEN)

    up = np.zeros((pre.size + tx_syms.size) * SPS, dtype=complex)
    up[::SPS] = np.concatenate([pre, tx_syms])
    rrc = np.asarray(firdes.root_raised_cosine(1.0, float(SPS), 1.0, ROLLOFF, 8 * SPS + 1))
    wave = np.fft.ifft(np.fft.fft(up) * np.fft.fft(rrc, up.size))   # circular conv -> periodic

    cap = np.tile(wave, 4)
    cap = cap * np.exp(2j * np.pi * (cfo_cyc_sym / SPS) * np.arange(cap.size))
    rng = np.random.default_rng(seed)
    sigma = np.sqrt(np.mean(np.abs(cap) ** 2) / (2 * 10 ** (snr_db / 10.0)))
    cap = cap + sigma * (rng.standard_normal(cap.size) + 1j * rng.standard_normal(cap.size))

    mf = np.fft.ifft(np.fft.fft(cap) * np.fft.fft(rrc, cap.size))
    sym = mf[::SPS]
    return sym / (np.sqrt(np.mean(np.abs(sym) ** 2)) + 1e-12), tx_bits, tx_syms, pre


def _ber(backend, cfo, snr_db=20.0):
    const = modulation.get(MOD)
    sym, tx_bits, tx_syms, pre = _stream(cfo, snr_db)
    rx_syms, length, dbg = backend._framesync_dd(sym, pre, tx_syms, const.points)
    rx_bits, _ = const.hard_decision(rx_syms)
    ref = tx_bits[: length * const.bits_per_symbol]
    return float(np.mean(rx_bits[: ref.size] != ref)), dbg


# CFOs spanning several sinc nulls of a coherent 256-symbol correlation (1, 2, 3 and 5 cycles).
@pytest.mark.parametrize("cfo", [0.0, 0.001, 0.002, BENCH_CFO_CYC_SYM, 0.008, 0.012, 0.020])
def test_framesync_dd_acquires_across_cfo(cfo):
    """16-QAM must acquire at any CFO in the bench's range -- not just between the sinc nulls."""
    ber, dbg = _ber(PlutoBackend("x", "y"), cfo)
    assert dbg["frame_sync_tone"] > 20.0, f"no preamble accepted at cfo={cfo} (link reads as dead)"
    assert ber < 1e-2, f"BER {ber:.2e} at cfo={cfo}"


def test_bench_cfo_regressed_with_fully_coherent_search():
    """Pin the actual bug: the pre-fix single-block search dies at exactly the bench CFO, while
    the shipped default recovers the same stream. If this ever passes with nb=1, the sinc-null
    failure mode has been reintroduced somewhere else."""
    ber_old, dbg_old = _ber(PlutoBackend("x", "y", framesync_blocks=1), BENCH_CFO_CYC_SYM)
    assert dbg_old["frame_sync_tone"] == 0.0 and ber_old > 0.3, "expected the old coherent null"

    ber_new, dbg_new = _ber(PlutoBackend("x", "y"), BENCH_CFO_CYC_SYM)
    assert dbg_new["frame_sync_tone"] > 20.0 and ber_new < 1e-2


def test_zero_cfo_unaffected_by_the_fix():
    """Partial-coherent detection must not cost anything in the easy case the old path handled."""
    ber_old, _ = _ber(PlutoBackend("x", "y", framesync_blocks=1), 0.0)
    ber_new, _ = _ber(PlutoBackend("x", "y"), 0.0)
    assert ber_old < 1e-2 and ber_new < 1e-2
