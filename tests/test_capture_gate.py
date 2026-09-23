"""The receiver's capture gate.

This exists because of an operational failure that no functional test could see: every measurement
was correct, every assertion passed, and the worker still reached 11 GB resident in under two hours
and took the machine into swap.

The cause is the interaction of two things that are each individually right. The gr-iio flowgraph
must be persistent, because device contexts hang on destruction. And ``blocks.vector_sink``
accumulates every sample it is ever handed. Together they mean the receiver fills a Python list at
the full sample rate -- ~16.7 MB/s at 2.084 Msps -- for the whole time an agent spends thinking
between trials, which on an LLM-driven loop is most of the wall clock. Resetting the sink around a
capture does not help: the idle gaps between captures are where the samples pile up.

So the gate is closed by default and opened only inside a measurement window. These tests hold that
property without needing radios, because the failure it prevents is not reproducible in a test
suite -- it needs hours and a real receiver.
"""
import pytest


class _FakeGate:
    """Stands in for ``blocks.copy``: records every enable/disable so the test can assert the gate
    is closed on every exit path."""

    def __init__(self):
        self.enabled = None
        self.history = []

    def set_enabled(self, value):
        self.enabled = bool(value)
        self.history.append(bool(value))


class _FakeSink:
    """Stands in for ``blocks.vector_sink_c``. Yields samples only while the gate is open, which is
    what the real block does once nothing is forwarded to it."""

    def __init__(self, gate, per_poll=4096):
        self._gate = gate
        self._per_poll = per_poll
        self._data = []
        self.resets = 0

    def data(self):
        if self._gate.enabled:
            self._data.extend([0j] * self._per_poll)
        return self._data

    def reset(self):
        self._data = []
        self.resets += 1


def _backend(gate, sink, rate=2_084_000.0):
    """A PlutoBackend with only the fields _gated_capture touches, so the method under test runs
    without constructing a flowgraph or opening a radio."""
    from gr_autopilot.link.pluto import PlutoBackend

    be = PlutoBackend.__new__(PlutoBackend)
    be._rxgate = gate
    be._rxsnk = sink
    be._running_cfg = rate
    return be


def test_the_gate_is_closed_before_and_after_a_capture():
    gate = _FakeGate()
    sink = _FakeSink(gate)
    be = _backend(gate, sink)

    out = be._gated_capture(4096)

    assert len(out) == 4096
    assert gate.history == [True, False], "the gate must open for the window and close after it"
    assert gate.enabled is False


def test_the_gate_closes_even_when_the_capture_fails():
    """The path that matters most. An exception leaving the gate open would refill the sink at the
    full sample rate for the rest of the session -- the original failure, re-armed."""
    gate = _FakeGate()

    class _StarvedSink(_FakeSink):
        def data(self):
            return []          # never reaches the requested count -> RuntimeError

    be = _backend(gate, _StarvedSink(gate), rate=1e9)   # huge rate keeps the deadline short

    with pytest.raises(RuntimeError, match="captured"):
        be._gated_capture(4096)

    assert gate.enabled is False, "a failed capture must still close the gate"
    assert gate.history[-1] is False


def test_the_sink_is_emptied_around_every_window():
    """Bounding what the sink holds is what keeps `data()` affordable: it copies the whole
    accumulated buffer on every call, so an unbounded buffer makes polling quadratic."""
    gate = _FakeGate()
    sink = _FakeSink(gate)
    be = _backend(gate, sink)
    be._gated_capture(4096)
    assert sink.resets >= 2, "cleared before opening and after closing"


def test_the_gate_starts_closed_on_a_freshly_built_flowgraph():
    """Read from the source rather than asserted in prose: the gate must be constructed disabled,
    or the very first idle period -- before any capture -- accumulates."""
    import pathlib

    src = pathlib.Path("gr_autopilot/link/pluto.py").read_text()
    i = src.index("rxgate = blocks.copy(")
    window = src[i:i + 200]
    assert "rxgate.set_enabled(False)" in window, (
        "the capture gate must be disabled at construction")
