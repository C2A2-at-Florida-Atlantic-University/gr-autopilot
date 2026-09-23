"""GrSimBackend tests: the real GNU Radio flowgraph path (needs GNU Radio).

Skipped automatically if GNU Radio is not importable. These check the flowgraph +
alignment + scoring behave sanely (clean link is error-free; more channel noise means
worse BER and lower measured SNR) rather than matching exact theory — the pure-numpy
backend owns the exact-theory check.
"""
import pytest

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring.metrics import compute_metrics

gr = pytest.importorskip("gnuradio", reason="GNU Radio not installed")
from gr_autopilot.link.gr_sim import GrSimBackend  # noqa: E402

pytestmark = pytest.mark.gnuradio


def _run(mod, noise_voltage, n_bits=100_000, sps=4):
    r = GrSimBackend().run_link(
        LinkParams(modulation=mod, n_payload_bits=n_bits, sps=sps, noise_voltage=noise_voltage)
    )
    return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)


@pytest.mark.parametrize("mod", ["bpsk", "qpsk", "16qam"])
def test_clean_link_error_free(mod):
    # No channel noise: matched RRC filters + alignment must recover every bit.
    assert _run(mod, noise_voltage=0.0).ber == 0.0


def test_ber_increases_with_noise():
    bers = [_run("qpsk", nv).ber for nv in (0.0, 0.3, 0.6, 1.0)]
    assert bers[0] == 0.0
    assert bers[-1] > bers[0]
    # Non-decreasing overall trend (allow tiny statistical wobble between adjacent points).
    assert bers[-1] >= bers[1]


def test_measured_snr_decreases_with_noise():
    snr_low = _run("qpsk", noise_voltage=0.1).snr_db
    snr_high = _run("qpsk", noise_voltage=0.8).snr_db
    assert snr_low > snr_high


def test_freq_offset_not_supported_yet():
    with pytest.raises(NotImplementedError):
        GrSimBackend().run_link(
            LinkParams(modulation="qpsk", n_payload_bits=1000, noise_voltage=0.0, freq_offset_hz=1000.0)
        )
