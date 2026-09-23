"""Supervising the radio worker, and presenting it as an ordinary link backend.

Two classes:

:class:`RadioWorker`
    Starts the worker process, exchanges requests with it, and -- the point of the whole design --
    notices when it dies. A radio that vanishes takes its worker with it (see
    :mod:`gr_autopilot.hardware.radio_worker` for why that cannot be caught), so the worker's
    death is the liveness signal rather than a failure to be prevented.

:class:`WorkerBackend`
    The same object wearing the :class:`~gr_autopilot.link.backend.LinkBackend` interface, so the
    service, the controllers and the tools drive a radio in a child process exactly as they drive
    one in the current process.

The behaviour that matters when a device is lost is what does NOT happen: no measurement is
recorded. A stalled receiver still hands back buffers, and those buffers grade as an error ratio
around one half -- indistinguishable from a jammed link. Recording that would put a fabricated
measurement into the experiment history and, worse, teach the agent that the channel got worse
when in fact a cable came out. So a lost device raises :class:`DeviceUnavailable`, which the tool
surface reports as an error, and the iteration is recorded as aborted rather than measured.
"""
from __future__ import annotations

import logging

import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from gr_autopilot.hardware.protocol import (
    CLOSE, DEVICE_HEALTH, PING, RUN_LINK, SENSE, SET_CONDITION, dumps, loads)
from gr_autopilot.link.backend import LinkBackend, LinkParams, LinkResult

_log = logging.getLogger("gr_autopilot.worker")


class DeviceUnavailable(RuntimeError):
    """A radio this experiment needs is not available.

    Deliberately distinct from an ordinary measurement failure: a link measured as poor is a
    result, whereas this is the absence of a result.
    """

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


class WorkerError(RuntimeError):
    """The worker was alive and answered, but the request itself failed.

    Kept separate from :class:`DeviceUnavailable` on purpose. A bad argument or a fault in the
    backend is a defect to fix; a missing radio is a physical condition to report and wait out.
    Treating the first as the second would hide real bugs behind a plausible hardware excuse and
    write spurious "device lost" entries into the experiment history.
    """

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


#: Error codes from the worker that mean the radio itself is gone, rather than the request being
#: at fault.
_DEVICE_CODES = {"backend_unavailable", "device_lost"}


@dataclass
class WorkerStatus:
    running: bool
    pid: int | None = None
    returncode: int | None = None
    died_at: float | None = None
    reason: str = ""
    devices: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        signal_name = ""
        if self.returncode is not None and self.returncode < 0:
            try:
                import signal as _signal
                signal_name = _signal.Signals(-self.returncode).name
            except (ValueError, ImportError):
                signal_name = f"signal {-self.returncode}"
        return {"running": self.running, "pid": self.pid, "returncode": self.returncode,
                "signal": signal_name, "died_at": self.died_at, "reason": self.reason,
                "devices": dict(self.devices)}


class RadioWorker:
    """Owns one worker process and the conversation with it."""

    def __init__(self, *, backend: str = "pluto", tx_uri: str = "ip:192.168.2.1",
                 rx_uri: str = "ip:192.168.3.1", sample_rate: float = 2_084_000.0,
                 rx_gain_db: float = 40.0, tx_atten_db: float = 10.0, sync_mode: str = "zc",
                 pilot_spacing: int = 0,
                 start_timeout: float = 30.0, request_timeout: float = 120.0,
                 python: str | None = None):
        self.args = ["--backend", backend, "--tx-uri", tx_uri, "--rx-uri", rx_uri,
                     "--sample-rate", str(sample_rate), "--rx-gain", str(rx_gain_db),
                     "--tx-atten", str(tx_atten_db), "--sync-mode", sync_mode,
                     "--pilot-spacing", str(int(pilot_spacing))]
        self.tx_uri, self.rx_uri, self.backend_name = tx_uri, rx_uri, backend
        self.start_timeout = float(start_timeout)
        self.request_timeout = float(request_timeout)
        self.python = python or sys.executable
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._status = WorkerStatus(running=False, reason="not started")

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self.is_running():
            return
        self._proc = subprocess.Popen(
            [self.python, "-m", "gr_autopilot.hardware.radio_worker", *self.args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        line = self._readline(self.start_timeout)
        if line is None:
            self._mark_dead("worker produced no ready line")
            raise DeviceUnavailable("radio worker did not start", detail=self._status.as_dict())
        try:
            hello = loads(line)
        except ValueError:
            self._mark_dead(f"unparseable ready line: {line[:120]}")
            raise DeviceUnavailable("radio worker sent an unreadable greeting",
                                    detail=self._status.as_dict()) from None
        if not hello.get("ok"):
            err = (hello.get("error") or {}).get("message", "backend unavailable")
            self._mark_dead(err)
            raise DeviceUnavailable(f"radio unavailable: {err}", detail=self._status.as_dict())
        self._status = WorkerStatus(running=True, pid=self._proc.pid, reason="ready")
        _log.info("radio worker ready (pid %d): %s", self._proc.pid, " ".join(self.args))

    def is_running(self) -> bool:
        if self._proc is None:
            return False
        if self._proc.poll() is not None:
            self._collect_death()
            return False
        return True

    def wait_for_exit(self, timeout: float = 10.0) -> bool:
        """Block until the worker has exited, or ``timeout`` passes. Returns whether it exited.

        An aborting process is not reaped instantly on a system whose crash handler runs first --
        this host pipes core dumps to a collector, which adds well over a second before the exit
        status becomes visible to the parent. Anything that needs to observe the death (a health
        poll, a test) should wait on this rather than guess with a sleep.
        """
        if self._proc is None:
            return True
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        self._collect_death()
        return True

    def _collect_death(self) -> None:
        if not self._status.running and self._status.returncode is not None:
            return                                   # already recorded
        rc = self._proc.returncode if self._proc else None
        stderr = ""
        try:
            if self._proc and self._proc.stderr:
                stderr = self._proc.stderr.read() or ""
        except (OSError, ValueError):
            pass
        reason = self._explain(rc, stderr)
        self._status = WorkerStatus(running=False, pid=self._proc.pid if self._proc else None,
                                    returncode=rc, died_at=time.time(), reason=reason,
                                    devices=dict(self._status.devices))

    @staticmethod
    def _explain(returncode, stderr: str) -> str:
        tail = " | ".join(l for l in (stderr or "").strip().splitlines()[-3:] if l)[:300]
        if returncode is not None and returncode < 0:
            import signal as _signal
            try:
                name = _signal.Signals(-returncode).name
            except ValueError:
                name = f"signal {-returncode}"
            if name == "SIGABRT":
                return ("worker aborted (SIGABRT) — the input/output library terminates the "
                        "process when a radio's connection goes silent, which is what an "
                        f"unplugged cable looks like. {tail}").strip()
            return f"worker killed by {name}. {tail}".strip()
        return f"worker exited with code {returncode}. {tail}".strip()

    def _mark_dead(self, reason: str) -> None:
        rc = self._proc.poll() if self._proc else None
        _log.error("radio worker dead (pid %s, exit %s): %s",
                   self._proc.pid if self._proc else None, rc, reason)
        self._status = WorkerStatus(running=False, pid=self._proc.pid if self._proc else None,
                                    returncode=rc, died_at=time.time(), reason=reason)

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._proc.stdin.write(dumps({"op": CLOSE}) + "\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._status = WorkerStatus(running=False, pid=self._proc.pid, returncode=self._proc.poll(),
                                    reason="stopped")
        _log.info("radio worker stopped (pid %d)", self._proc.pid)
        self._proc = None

    # -- conversation --------------------------------------------------------
    def _readline(self, timeout: float) -> str | None:
        """Read one reply line, giving up after ``timeout``.

        A worker whose radio has gone silent can block for several seconds before the library
        gives up, and may then be killed outright, so a read must never wait for ever.
        """
        result: list[str] = []

        def reader():
            try:
                line = self._proc.stdout.readline()
                if line:
                    result.append(line)
            except (OSError, ValueError):
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        if result:
            return result[0].strip()
        return None

    def request(self, op: str, **body) -> dict:
        with self._lock:
            if not self.is_running():
                raise DeviceUnavailable(self._status.reason or "radio worker is not running",
                                        detail=self._status.as_dict())
            try:
                self._proc.stdin.write(dumps({"op": op, **body}) + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError, BrokenPipeError):
                self._collect_death()
                raise DeviceUnavailable(self._status.reason, detail=self._status.as_dict()) from None

            line = self._readline(self.request_timeout)
            if line is None:
                # Either the worker died mid-request (the usual case for a lost radio) or it is
                # wedged. Both mean no measurement exists.
                if not self.is_running():
                    raise DeviceUnavailable(self._status.reason, detail=self._status.as_dict())
                self._mark_dead("worker stopped responding")
                self.stop()
                raise DeviceUnavailable("radio worker stopped responding",
                                        detail=self._status.as_dict())
            reply = loads(line)
        if not reply.get("ok"):
            err = reply.get("error") or {}
            code = err.get("code", "")
            message = err.get("message", "worker error")
            if code in _DEVICE_CODES:
                raise DeviceUnavailable(message, detail=err)
            # The worker answered, so the radios are still there; the request was the problem.
            raise WorkerError(f"[{code}] {message}", detail=err)
        return reply

    # -- health --------------------------------------------------------------
    def status(self) -> WorkerStatus:
        self.is_running()
        return self._status

    def poll_devices(self) -> dict:
        """Per-device liveness.

        While the worker is alive it answers, because it is the process holding the radios. Once
        it has died nobody holds them, so each device is probed directly from here -- and that
        distinction matters: losing the receiver kills the worker, but the transmitter is
        usually still perfectly healthy. Reporting both as gone because one went would blank a
        working device on the dashboard and misdescribe the fault.
        """
        if not self.is_running():
            from gr_autopilot.hardware.health import probe_devices
            if self.backend_name != "pluto":
                # Nothing physical to probe (the simulated worker), so the worker's death is all
                # the information there is.
                gone = {"alive": False, "reason": self._status.reason}
                devices = {"tx": {"uri": self.tx_uri, **gone},
                           "rx": {"uri": self.rx_uri, **gone}}
            else:
                devices = probe_devices({"tx": self.tx_uri, "rx": self.rx_uri})
                for role, dev in devices.items():
                    dev["worker"] = "dead"
                    if not dev.get("alive"):
                        dev.setdefault("reason", self._status.reason)
            self._status.devices = devices
            return devices
        try:
            devices = self.request(DEVICE_HEALTH).get("health", {})
        except DeviceUnavailable as exc:
            devices = {"tx": {"uri": self.tx_uri, "alive": False, "reason": str(exc)},
                       "rx": {"uri": self.rx_uri, "alive": False, "reason": str(exc)}}
        self._status.devices = devices
        return devices

    def ping(self) -> bool:
        try:
            return bool(self.request(PING).get("pong"))
        except DeviceUnavailable:
            return False


class WorkerBackend(LinkBackend):
    """A :class:`LinkBackend` whose measurements happen in a child process."""

    def __init__(self, worker: RadioWorker, autostart: bool = True):
        self.worker = worker
        self.name = f"worker:{worker.backend_name}"
        # Whether the backend holds the channel condition as device state follows from WHICH
        # backend is in the worker, not from the fact that a worker is involved. A radio owns its
        # own gains; a simulator is handed the channel with every request. Hard-coding this to
        # true made the simulated worker reject every measurement for a missing channel.
        self.owns_channel = worker.backend_name == "pluto"
        # What the backend in the CHILD process can actually be told. Declared here because the
        # parent never imports it and so cannot ask (see LinkBackend.applies_condition). A key
        # missing from this set is dropped silently at the far end of the pipe, which is how a
        # hop plan came to be set on radios that never hopped.
        self.applies_condition = (
            frozenset({"center_freq_hz", "tx_atten_db", "rx_gain_db"})
            if worker.backend_name == "pluto" else frozenset())
        if autostart:
            worker.start()

    def run_link(self, params: LinkParams) -> LinkResult:
        payload = {k: v for k, v in vars(params).items()}
        payload["tx_chain"] = list(payload.get("tx_chain") or ())
        payload["rx_chain"] = list(payload.get("rx_chain") or ())
        reply = self.worker.request(RUN_LINK, params=payload)
        r = reply["result"]
        return LinkResult(
            modulation=r["modulation"],
            tx_bits=np.asarray(r["tx_bits"]), rx_bits=np.asarray(r["rx_bits"]),
            tx_syms=np.asarray(r["tx_syms"]), rx_syms=np.asarray(r["rx_syms"]),
            ref_syms=np.asarray(r["ref_syms"]), meta=dict(r.get("meta") or {}))

    def set_condition(self, **cond) -> None:
        self.worker.request(SET_CONDITION, condition=cond)

    def sense_spectrum(self, freqs_hz, bw_hz=None):
        return self.worker.request(SENSE, freqs_hz=list(freqs_hz), bw_hz=bw_hz).get("rows")

    def close(self) -> None:
        self.worker.stop()
