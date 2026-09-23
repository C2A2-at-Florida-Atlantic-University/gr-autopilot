"""The jammer as a daemon-owned instrument (operator side of the integrity boundary).

RESEARCH USE ONLY. This module keys a REAL transmitter through
:mod:`gr_autopilot.hardware.interferer`; read that module's warning and ``RESPONSIBLE-USE.md``
before arming it. It is for a closed, cabled, attenuated bench where nothing radiates.

Why this exists. The interferer has always been drivable from a script, which builds its own
service, keys the HackRF on a timeline written in advance, and drives an in-process agent. That
proves the mechanism but scripts the weather: the epochs are decided before the agent starts. This
wraps the same interferer as a long-lived instrument the daemon owns, so a human operator can arm,
move and disarm the jammer WHILE an external agent is mid-session and knows nothing about it.

Two rules follow from the integrity split (spec §2/§6) and are the reason this lives here rather
than on the tool surface:

* **The agent must never see it.** Not in ``list_devices``, not in ``get_status``, not as a tool.
  Every Stage-2/3 claim is that the agent discovers interference from measurement alone; an
  inventory entry would hand it the answer. The instrument is reported only on the operator's
  ``/devices`` view and driven only through the token-guarded operator channel.
* **A dead jammer must never read as a clean band.** ``HackRFInterferer.start`` raises if
  ``hackrf_transfer`` exits during settle, and :meth:`JammerInstrument.status` additionally
  reports a jammer that died *after* it -- mid-experiment death is the dangerous case, because
  the band genuinely goes quiet and the agent's "I found a clear channel" becomes a fiction.

One hazard is specific to running the interferer here rather than in a script. The interferer asks
the kernel for ``PR_SET_PDEATHSIG`` so a parent crash cannot leave a transmitter keyed -- but that
signal fires when the parent THREAD exits, not the parent process. A script starts the jammer on
its long-lived main thread and never notices; a threaded HTTP server would start it on a request
thread that exits seconds later, and the jammer dies with it (observed: ``hackrf_transfer`` logging
"Caught signal 15" moments after a successful arm). So every process-owning call is funnelled
through one long-lived owner thread, and the safety property is preserved rather than dropped.
"""
from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger("gr_autopilot.jammer")

#: HackRF One tuning range (Hz) and TX VGA range (dB) -- an operator command outside these is
#: refused rather than clamped, because a jammer quietly parked somewhere other than where the
#: operator believes it is invalidates the run it is part of.
FREQ_RANGE_HZ = (1e6, 6e9)
IF_GAIN_RANGE_DB = (0, 47)
KINDS = ("cw", "noise")

#: How often a following jammer re-reads the link's channel. The follower's own reaction time is
#: the kill/restart of ``hackrf_transfer`` (~1 s, measured); polling much faster than that would
#: only add scheduling noise to a latency the experiment is trying to measure.
FOLLOW_POLL_S = 0.25


class JammerError(RuntimeError):
    """An operator jammer command that was refused, with the reason."""


class JammerInstrument:
    """A HackRF jammer owned by the daemon and driven by the operator.

    ``enabled`` is the arming interlock: a daemon started without ``--jammer`` refuses every
    command outright, so a transmitter cannot be keyed by a stray POST to a server that was never
    meant to have one. ``link_freq`` is a callable returning the link's current centre frequency
    (or ``None``), used only when following.
    """

    def __init__(self, enabled: bool = False, sample_rate: int = 2_084_000,
                 link_freq=None, factory=None):
        self.enabled = bool(enabled)
        self.sample_rate = int(sample_rate)
        self._link_freq = link_freq
        self._factory = factory                  # injected in tests; real classes by default
        self._lock = threading.RLock()
        self._itf = None                         # the live HackRFInterferer / FollowerJammer
        self._cfg = {"center_freq_hz": 2.4e9, "if_gain": 20, "kind": "cw", "follow": False}
        self._error: str | None = None
        self._intended = False                   # what the operator last asked for
        self._follow_thread: threading.Thread | None = None
        self._follow_stop = threading.Event()
        # Every start/stop of hackrf_transfer runs here (see the PR_SET_PDEATHSIG note above).
        self._cmds: queue.Queue = queue.Queue()
        self._owner = threading.Thread(target=self._serve_commands, name="jammer-owner",
                                       daemon=True)
        self._owner.start()

    def _serve_commands(self) -> None:
        """Own every interferer subprocess for the life of the daemon."""
        while True:
            item = self._cmds.get()
            if item is None:
                return
            fn, slot = item
            try:
                slot["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller verbatim
                slot["error"] = exc
            finally:
                slot["done"].set()

    def _submit(self, fn, timeout: float = 30.0):
        """Run ``fn`` on the owner thread and re-raise whatever it raised."""
        slot = {"done": threading.Event()}
        self._cmds.put((fn, slot))
        if not slot["done"].wait(timeout):
            raise JammerError(f"jammer command did not complete within {timeout:g}s")
        if "error" in slot:
            raise slot["error"]
        return slot.get("value")

    # ---- capability ------------------------------------------------------

    def available(self) -> bool:
        """Is the HackRF CLI present? Reported separately from ``enabled`` so an operator can tell
        "this daemon was not started with a jammer" from "the tooling is missing"."""
        from gr_autopilot.hardware.interferer import HackRFInterferer
        return HackRFInterferer.available()

    # ---- operator commands ------------------------------------------------

    def apply(self, cmd: dict) -> dict:
        """Apply an operator command and return the resulting status.

        Recognised keys: ``jammer`` (bool, arm/disarm), ``jammer_freq_hz``, ``jammer_if_gain``,
        ``jammer_kind`` (``cw``/``noise``), ``jammer_follow`` (bool). Parameter changes while armed
        take effect immediately -- for a static jammer by restarting it, which is the only way a
        CLI-driven HackRF can change frequency at all.
        """
        with self._lock:
            want_on = bool(cmd.get("jammer", self.is_armed()))
            new = dict(self._cfg)
            if "jammer_freq_hz" in cmd:
                new["center_freq_hz"] = self._check_freq(cmd["jammer_freq_hz"])
            if "jammer_if_gain" in cmd:
                new["if_gain"] = self._check_gain(cmd["jammer_if_gain"])
            if "jammer_kind" in cmd:
                new["kind"] = self._check_kind(cmd["jammer_kind"])
            if "jammer_follow" in cmd:
                new["follow"] = bool(cmd["jammer_follow"])
            if (want_on or self.is_armed()) and not self.enabled:
                raise JammerError(
                    "this daemon has no jammer: restart it with --jammer to enable one "
                    "(it keys a real transmitter; see RESPONSIBLE-USE.md)")
            restart = self.is_armed() and new != self._cfg
            self._cfg = new
            self._intended = want_on
            if not want_on:
                self._disarm()
            elif not self.is_armed():
                self._arm()
            elif restart:
                self._rearm()
            return self.status()

    # ---- validation (refuse, never clamp) ---------------------------------

    @staticmethod
    def _check_freq(v) -> float:
        f = float(v)
        lo, hi = FREQ_RANGE_HZ
        if not (lo <= f <= hi):
            raise JammerError(f"jammer_freq_hz {f:g} outside the HackRF range {lo:g}-{hi:g} Hz")
        return f

    @staticmethod
    def _check_gain(v) -> int:
        g = int(v)
        lo, hi = IF_GAIN_RANGE_DB
        if not (lo <= g <= hi):
            raise JammerError(f"jammer_if_gain {g} outside the TX VGA range {lo}-{hi} dB")
        return g

    @staticmethod
    def _check_kind(v) -> str:
        k = str(v)
        if k not in KINDS:
            raise JammerError(f"jammer_kind {k!r} unknown; expected one of {', '.join(KINDS)}")
        return k

    # ---- lifecycle --------------------------------------------------------

    def _new_interferer(self):
        if self._factory is not None:
            return self._factory(**self._cfg, sample_rate=self.sample_rate)
        from gr_autopilot.hardware.interferer import FollowerJammer, HackRFInterferer
        cls = FollowerJammer if self._cfg["follow"] else HackRFInterferer
        return cls(center_freq_hz=self._cfg["center_freq_hz"], if_gain=self._cfg["if_gain"],
                   kind=self._cfg["kind"], sample_rate=self.sample_rate)

    def _arm(self) -> None:
        self._error = None
        itf = self._submit(self._new_interferer)
        try:
            self._submit(itf.start)
        except Exception as exc:  # noqa: BLE001 - a launch failure must be reported, never hidden
            self._error = str(exc)
            log.error("jammer failed to start: %s", exc)
            try:
                self._submit(itf.close)
            except Exception:  # noqa: BLE001
                pass
            raise JammerError(f"jammer did not start: {exc}") from exc
        self._itf = itf
        # WARNING, not INFO: this is a transmitter keying in a shared chamber, and it must stand
        # out on a console that is otherwise scrolling tool calls.
        log.warning("interferer ARMED: %s at %.3f MHz, if_gain %d%s", self._cfg["kind"],
                    self._cfg["center_freq_hz"] / 1e6, self._cfg["if_gain"],
                    " (following the link)" if self._cfg["follow"] else "")
        if self._cfg["follow"]:
            self._start_following()

    def _disarm(self) -> None:
        self._stop_following()
        if self._itf is not None:
            itf, self._itf = self._itf, None
            self._submit(itf.close)    # full teardown: stop the TX and drop the temp wave dir
            log.info("interferer disarmed")

    def _rearm(self) -> None:
        self._disarm()
        self._arm()

    def is_armed(self) -> bool:
        return self._itf is not None and self._itf.running()

    # ---- following --------------------------------------------------------

    def _start_following(self) -> None:
        if self._link_freq is None or self._follow_thread is not None:
            return
        self._follow_stop.clear()
        self._follow_thread = threading.Thread(target=self._follow_loop, daemon=True,
                                               name="jammer-follow")
        self._follow_thread.start()

    def _stop_following(self) -> None:
        self._follow_stop.set()
        t, self._follow_thread = self._follow_thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)

    def _follow_loop(self) -> None:
        """Chase the link's channel. The retune cost (kill + relaunch ``hackrf_transfer``) IS the
        follower's reaction latency and is exactly what the link out-hops -- so it is measured, not
        engineered away."""
        while not self._follow_stop.wait(FOLLOW_POLL_S):
            try:
                f = self._link_freq()
            except Exception:  # noqa: BLE001 - the link may be mid-reconfiguration
                continue
            if f is None:
                continue
            with self._lock:
                itf = self._itf
                if itf is None or not hasattr(itf, "retune") or float(f) == itf.center_freq_hz:
                    continue
                try:
                    self._submit(lambda: itf.retune(float(f)))
                    self._cfg["center_freq_hz"] = float(f)
                except Exception as exc:  # noqa: BLE001
                    self._error = f"follower could not retune: {exc}"
                    log.error("%s", self._error)

    # ---- reporting (operator only) ----------------------------------------

    def status(self) -> dict:
        with self._lock:
            itf = self._itf
            armed = self.is_armed()
            if self._intended and not armed and self._error is None:
                # It started, then stopped. The band is quiet again and nothing else would say so.
                self._error = ("jammer exited on its own after starting — it is NOT transmitting; "
                               "any 'clear channel' measured since is not evidence of avoidance")
                log.error("%s", self._error)
            st = {
                "enabled": self.enabled,
                "available": self.available() if self.enabled else None,
                "armed": armed,
                "intended": self._intended,
                "error": self._error,
                **self._cfg,
            }
            if itf is not None and hasattr(itf, "retune_count"):
                st["retune_count"] = itf.retune_count
                st["last_retune_latency_s"] = round(itf.last_latency_s, 3)
            return st

    def shutdown(self) -> None:
        """Unconditional teardown. Wired to daemon exit and to SIGTERM/SIGINT: a process holding a
        keyed transmitter must not be able to die and leave it transmitting."""
        with self._lock:
            self._intended = False
            try:
                self._disarm()
            except Exception:  # noqa: BLE001
                pass
        self._cmds.put(None)          # retire the owner thread last, so nothing outlives it
