"""Isolating the radios in a child process, and treating that child's death as information.

These tests use the simulated backend inside a real subprocess, so the whole path -- spawn,
request, reply, death, recovery -- is exercised without any radios attached.

The property being defended is not "the worker works". It is what happens when it DOESN'T: a lost
radio must produce no measurement at all. A stalled receiver still hands back buffers, and those
buffers grade as an error ratio near one half, which is indistinguishable from a jammed link.
Recording that would write a fabricated result into the experiment history and teach the agent
that the channel degraded when in fact a cable came out.
"""
import os
import signal
import time

import numpy as np
import pytest

from gr_autopilot.hardware.protocol import decode_array, encode_array, dumps, loads
from gr_autopilot.hardware.supervisor import DeviceUnavailable, RadioWorker, WorkerBackend
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring import metrics

PARAMS = dict(modulation="qpsk", n_payload_bits=20000, es_n0_db=12.0, seed=1)


@pytest.fixture()
def worker():
    w = RadioWorker(backend="sim", start_timeout=30.0, request_timeout=60.0)
    yield w
    w.stop()


# -- the wire format ---------------------------------------------------------

def test_arrays_survive_the_round_trip_exactly():
    """A trial produces tens of thousands of values; they are carried as raw buffers rather than
    decimal text, so this checks dtype, shape and content all survive."""
    for arr in (np.arange(10, dtype=np.int8),
                np.linspace(0, 1, 32).astype(np.float32),
                (np.random.default_rng(0).standard_normal(16)
                 + 1j * np.random.default_rng(1).standard_normal(16))):
        back = decode_array(encode_array(arr))
        assert back.dtype == arr.dtype and back.shape == arr.shape
        assert np.array_equal(back, arr)


def test_encoding_round_trips_through_the_message_format():
    payload = {"result": {"rx_syms": np.array([1 + 2j, 3 - 4j]), "meta": {"sps": 8}}}
    back = loads(dumps(payload))
    assert np.array_equal(back["result"]["rx_syms"], payload["result"]["rx_syms"])
    assert back["result"]["meta"]["sps"] == 8


# -- normal operation --------------------------------------------------------

def test_a_measurement_crosses_the_process_boundary_intact(worker):
    backend = WorkerBackend(worker)
    result = backend.run_link(LinkParams(**PARAMS))
    m = metrics.compute_metrics(result.tx_bits, result.rx_bits, result.rx_syms, result.ref_syms)
    assert m.n_bits == 20000
    assert result.tx_bits.dtype == np.int8
    assert np.iscomplexobj(result.rx_syms)


def test_the_worker_runs_in_its_own_process(worker):
    WorkerBackend(worker)
    assert worker.status().pid not in (None, os.getpid())


def test_repeated_measurements_reuse_one_worker(worker):
    backend = WorkerBackend(worker)
    pid = worker.status().pid
    for _ in range(3):
        backend.run_link(LinkParams(**PARAMS))
    assert worker.status().pid == pid


# -- death -------------------------------------------------------------------

def test_an_aborted_worker_yields_no_measurement(worker):
    """SIGABRT is exactly how a silently-disconnected radio ends the process that owns it."""
    backend = WorkerBackend(worker)
    backend.run_link(LinkParams(**PARAMS))
    os.kill(worker.status().pid, signal.SIGABRT)
    assert worker.wait_for_exit(timeout=20.0)

    with pytest.raises(DeviceUnavailable) as caught:
        backend.run_link(LinkParams(**PARAMS))
    assert caught.value.detail["signal"] == "SIGABRT"
    assert caught.value.detail["returncode"] == -signal.SIGABRT


def test_the_death_is_explained_in_terms_an_operator_can_act_on(worker):
    WorkerBackend(worker)
    os.kill(worker.status().pid, signal.SIGABRT)
    assert worker.wait_for_exit(timeout=20.0)
    reason = worker.status().reason
    assert "SIGABRT" in reason and "silent" in reason


def test_the_supervising_process_survives_the_worker(worker):
    """The reason the split exists: the process serving the dashboard and the tool surface must
    outlive any radio attached to it."""
    backend = WorkerBackend(worker)
    os.kill(worker.status().pid, signal.SIGKILL)
    assert worker.wait_for_exit(timeout=20.0)
    with pytest.raises(DeviceUnavailable):
        backend.run_link(LinkParams(**PARAMS))
    assert os.getpid()          # reached at all == this process is still running
    assert not worker.is_running()


def test_a_dead_worker_reports_every_device_it_owned_as_unavailable(worker):
    WorkerBackend(worker)
    os.kill(worker.status().pid, signal.SIGABRT)
    assert worker.wait_for_exit(timeout=20.0)
    devices = worker.poll_devices()
    assert set(devices) == {"tx", "rx"}
    assert not any(d["alive"] for d in devices.values())


def test_device_unavailable_is_not_an_ordinary_measurement_failure():
    """A poor link is a result; a missing radio is the absence of one. They must not be the same
    exception, or a controller cannot tell "this modulation is too ambitious" from "the receiver
    is gone" -- and would respond to a cable fault by dropping the data rate."""
    assert issubclass(DeviceUnavailable, RuntimeError)
    assert not issubclass(DeviceUnavailable, ValueError)


# -- restart -----------------------------------------------------------------

def test_a_worker_can_be_restarted_after_a_death(worker):
    backend = WorkerBackend(worker)
    first = worker.status().pid
    os.kill(first, signal.SIGABRT)
    assert worker.wait_for_exit(timeout=20.0)
    with pytest.raises(DeviceUnavailable):
        backend.run_link(LinkParams(**PARAMS))

    worker.start()                       # the device came back
    assert worker.is_running() and worker.status().pid != first
    result = backend.run_link(LinkParams(**PARAMS))
    assert result.rx_bits.size > 0


def test_starting_an_already_running_worker_is_a_no_op(worker):
    WorkerBackend(worker)
    pid = worker.status().pid
    worker.start()
    assert worker.status().pid == pid


def test_an_unavailable_backend_is_reported_rather_than_hanging():
    """Asking for a radio that is not there must fail quickly and say so."""
    w = RadioWorker(backend="pluto", tx_uri="ip:203.0.113.1", rx_uri="ip:203.0.113.2",
                    start_timeout=25.0)
    with pytest.raises(DeviceUnavailable):
        w.start()
    w.stop()


# -- health ------------------------------------------------------------------

def test_health_reports_nothing_physical_for_a_simulated_worker(worker):
    """The simulated backend has no radios, so there is nothing to probe. An empty map is the
    honest answer -- inventing two healthy devices would be a lie the dashboard would display."""
    WorkerBackend(worker)
    assert worker.poll_devices() == {}


def test_ping_answers_while_alive_and_not_after(worker):
    WorkerBackend(worker)
    assert worker.ping() is True
    os.kill(worker.status().pid, signal.SIGKILL)
    assert worker.wait_for_exit(timeout=20.0)
    assert worker.ping() is False
