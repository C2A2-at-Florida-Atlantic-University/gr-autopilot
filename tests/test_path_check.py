"""Tier-B path verification: the check must transmit, judge the median of several senses, and
never leave the interferer keyed. Everything the radios would do is faked here."""
from types import SimpleNamespace

import pytest

import gr_autopilot.hardware.path_check as pc


class FakeJammer:
    enabled = True

    def __init__(self):
        self.calls = []

    def apply(self, cmd):
        self.calls.append(dict(cmd))

    def shutdown(self):
        self.calls.append("shutdown")


def _rows(margin_db):
    return [{"center_freq_hz": f, "power_db": (margin_db if f == 2.37e9 else 0.0),
             "peak_db": (margin_db if f == 2.37e9 else 0.0)}
            for f in (2.364e9, 2.37e9, 2.376e9, 2.382e9)]


def _service(sense_sequence):
    it = iter(sense_sequence)
    backend = SimpleNamespace(sense_spectrum=lambda freqs, bw: _rows(next(it)))
    return SimpleNamespace(backend=backend, current_link_freq=lambda: 2.37e9,
                           _channel_kwargs=lambda: {})


@pytest.fixture(autouse=True)
def _good_link(monkeypatch):
    """A healthy BPSK link: 18 dB, BER 1e-4. The interferer arm is what these tests exercise."""
    import gr_autopilot.flowgraph as fg
    import gr_autopilot.scoring.metrics as metrics
    monkeypatch.setattr(fg, "build_and_run", lambda *a, **k: SimpleNamespace(
        tx_bits=None, rx_bits=None, rx_syms=None, ref_syms=None))
    monkeypatch.setattr(metrics, "compute_metrics",
                        lambda *a, **k: SimpleNamespace(snr_db=18.0, ber=1e-4))


def test_median_of_several_senses_is_judged_and_spread_reported():
    j = FakeJammer()
    out = pc.verify_path(_service([19.6, 19.7, 18.8]), jammer=j, expected_snr_db=20.0)
    assert out.link_ok and out.jammer_ok and out.ok
    assert out.jammer_margin_db == 19.6
    assert out.jammer_margin_spread_db == pytest.approx(0.9)
    assert out.findings == []


def test_pulsed_jammer_fails_on_spread_even_when_the_median_passes():
    """The 2026-09-08 failure mode: +21 on one sense, +3 on the next. Median 21 would pass."""
    j = FakeJammer()
    out = pc.verify_path(_service([21.0, 3.4, 21.0]), jammer=j)
    assert out.jammer_margin_db == 21.0
    assert out.jammer_margin_spread_db == pytest.approx(17.6)
    assert out.jammer_ok is False and out.ok is False
    assert any("unstable" in f and "loop_length" in f for f in out.findings)


def test_absent_jammer_fails_on_the_median():
    out = pc.verify_path(_service([4.0, 5.0, 4.5]), jammer=FakeJammer())
    assert out.jammer_ok is False
    assert any("does not reach the receiver" in f and "median of 3" in f for f in out.findings)


def test_interferer_is_disarmed_after_the_check_whatever_happens():
    j = FakeJammer()
    pc.verify_path(_service([19.0, 19.0, 19.0]), jammer=j)
    assert j.calls[0]["jammer"] is True and j.calls[-1] == {"jammer": False}
    j2 = FakeJammer()
    pc.verify_path(_service([19.0]), jammer=j2)          # second sense raises StopIteration
    assert j2.calls[-1] == {"jammer": False}


def test_to_dict_carries_the_spread():
    out = pc.verify_path(_service([19.0, 20.0, 19.5]), jammer=FakeJammer())
    d = out.to_dict()
    assert d["jammer_margin_db"] == 19.5 and d["jammer_margin_spread_db"] == 1.0
