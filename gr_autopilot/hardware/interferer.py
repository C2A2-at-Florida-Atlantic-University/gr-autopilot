"""HackRF interferer — a framework-controlled jammer over the wired path (Stage 2).

RESEARCH USE ONLY. This module KEYS A REAL TRANSMITTER. It may be run only into a CONTAINED
environment: a closed cabled path with attenuators, or a SHIELDED enclosure. Containment is what
matters, not the absence of an antenna — antennas are normal inside a screened chamber and
catastrophic outside one. Note that "anechoic" is not "shielded": absorber suppresses reflections
and does nothing to stop energy leaving the room.

Transmitting an interfering signal over the air is illegal in nearly every jurisdiction (in the US,
47 U.S.C. §333) and can disrupt safety-of-life services. The gain defaults below are conservative
values calibrated for a 20 dB-attenuated cabled path; over the air inside a chamber the coupling is
set by geometry instead, so they are neither calibrated nor a safety mechanism. The interlock that
actually gates this is the operator attestation in the bench topology
(:meth:`gr_autopilot.hardware.topology.Topology.jammer_permitted`). See RESPONSIBLE-USE.md.

Drives a HackRF One via the native ``hackrf_transfer -R`` tool (gr-osmosdr/SoapyHackRF are not
built on the bench host) streaming a looped IQ file as a continuous in-band interferer. The
operator/framework turns it on and sets its frequency and strength; the AGENT is never told
where it is and must adapt (frequency avoidance) from measured BER alone.

Role split (spec §6): the interferer is a framework participant, not something the agent claims
or controls — it lives on the framework's side of the integrity boundary like the grader.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np


def _pdeathsig() -> None:
    """Ask the kernel to SIGTERM this child if the parent dies (Linux PR_SET_PDEATHSIG), so a hard
    parent crash before stop() can't leave hackrf_transfer -R transmitting forever. Best-effort."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)
    except Exception:
        pass


#: hackrf_transfer streams the IQ file one USB transfer at a time (262,144 bytes = 131,072 int8
#: I/Q pairs per callback) and reads the file ONCE per transfer: at end-of-file with ``-R`` it
#: rewinds and PADS the rest of that transfer rather than looping the file to fill it. A file
#: shorter than one transfer is therefore transmitted as a pulse train, not a continuous signal.
#: The 10 ms (20,840-sample) tone used until 2026-09-08 keyed a 16 %-duty tone at a 63 ms period:
#: measured 16 rewinds/s, and the receiver's margin flipping between +3 and +21 dB from one sense
#: to the next depending on where the capture landed. A 0.5 s file read a flat +22 dB.
HACKRF_TRANSFER_SAMPLES = 131_072


def loop_length(n_samples: int) -> int:
    """Sample count for a wave file that hackrf_transfer -R streams continuously: rounded UP to a
    whole number of USB transfers, and at least eight of them (~0.5 s at 2.084 MS/s, 2 MiB).

    Measured 2026-09-08 (2.084 MS/s CW, gain 20, chamber receiver, 30 senses each): the old
    20,840-sample default flipped between +3 and +21 dB; eight whole transfers read a flat
    +21..22 dB, as did eight transfers less one sample. Whole transfers keep the loop seam on a
    transfer boundary, and the length keeps any rewind padding below 1 % of the loop.
    """
    n = max(int(n_samples), 8 * HACKRF_TRANSFER_SAMPLES)
    return -(-n // HACKRF_TRANSFER_SAMPLES) * HACKRF_TRANSFER_SAMPLES


def write_cw_tone(path: str, sample_rate: int, offset_hz: float, amp: int = 18,
                  n_samples: int = 8 * HACKRF_TRANSFER_SAMPLES) -> None:
    """Write a seamless looped CW tone as HackRF int8 I/Q-interleaved samples.

    The loop is glitch-free when an integer number of tone cycles fits in ``n_samples``; the
    offset is snapped to the nearest bin (``sample_rate / n_samples`` Hz) to guarantee that.
    ``n_samples`` is rounded by :func:`loop_length` so the file streams continuously.
    """
    n_samples = loop_length(n_samples)
    k = round(offset_hz / (sample_rate / n_samples))
    n = np.arange(n_samples)
    iq = np.empty(2 * n_samples, dtype=np.int8)
    iq[0::2] = np.round(amp * np.cos(2 * np.pi * k * n / n_samples)).astype(np.int8)
    iq[1::2] = np.round(amp * np.sin(2 * np.pi * k * n / n_samples)).astype(np.int8)
    iq.tofile(path)


def write_band_noise(path: str, sample_rate: int, bandwidth_hz: float, rms: float = 24.0,
                     n_samples: int = 8 * HACKRF_TRANSFER_SAMPLES, seed: int = 0) -> None:
    """Write looped band-limited complex noise (partial-band jammer) as HackRF int8 I/Q.

    White Gaussian noise brick-wall filtered to ``+-bandwidth/2`` around DC, scaled to a target
    RMS and clipped to int8. Repeating the buffer makes it periodic, but over ``n_samples`` the
    line spacing is fine enough to read as band-limited noise for jamming. ``n_samples`` is
    rounded by :func:`loop_length` so the file streams continuously.
    """
    n_samples = loop_length(n_samples)
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n_samples) + 1j * rng.standard_normal(n_samples)
    X = np.fft.fft(x)
    fr = np.fft.fftfreq(n_samples, 1.0 / sample_rate)
    X[np.abs(fr) > bandwidth_hz / 2.0] = 0.0
    y = np.fft.ifft(X)
    y = y / (np.std(y) + 1e-12) * rms
    iq = np.empty(2 * n_samples, dtype=np.int8)
    iq[0::2] = np.clip(np.round(y.real), -127, 127).astype(np.int8)
    iq[1::2] = np.clip(np.round(y.imag), -127, 127).astype(np.int8)
    iq.tofile(path)


class HackRFInterferer:
    """A HackRF One streaming a continuous in-band CW tone as a jammer.

    ``center_freq_hz`` is the HackRF LO; the tone sits at ``center_freq_hz + tone_offset_hz`` (a
    small offset keeps it off the HackRF's DC/LO-leakage spike while staying inside the link's
    passband). Strength is set by ``tone_amp`` (digital, int8 full-scale 127), ``if_gain``
    (HackRF TX VGA, 0-47 dB), and ``amp_enable`` (RF amp, +14 dB). Use as a context manager.
    """

    def __init__(self, center_freq_hz: float = 2.4e9, sample_rate: int = 2_084_000,
                 tone_offset_hz: float = 100_000.0, tone_amp: int = 18,
                 if_gain: int = 20, amp_enable: int = 0, settle_s: float = 0.8,
                 kind: str = "cw", bandwidth_hz: float = 1_000_000.0):
        self.center_freq_hz = float(center_freq_hz)
        self.sample_rate = int(sample_rate)
        self.tone_offset_hz = float(tone_offset_hz)
        self.tone_amp = int(tone_amp)
        self.if_gain = int(if_gain)
        self.amp_enable = int(amp_enable)
        self.settle_s = float(settle_s)
        self.kind = str(kind)               # "cw" tone or "noise" (band-limited, partial-band)
        self.bandwidth_hz = float(bandwidth_hz)
        self._proc: subprocess.Popen | None = None
        self._err_fh = None                              # stderr capture, for launch-failure reports
        self._tmpdir = tempfile.mkdtemp(prefix="gr_autopilot_itf_")
        self._wave = os.path.join(self._tmpdir, "jam.c8")

    @staticmethod
    def available() -> bool:
        return shutil.which("hackrf_transfer") is not None

    def start(self) -> None:
        if self._proc is not None:
            return
        if self.kind == "noise":
            write_band_noise(self._wave, self.sample_rate, self.bandwidth_hz)
        else:
            write_cw_tone(self._wave, self.sample_rate, self.tone_offset_hz, self.tone_amp)
        # Capture stderr to a file (not DEVNULL) so a failed launch can be reported. hackrf_transfer
        # -R loops forever; a healthy jammer never exits, so a non-None poll() after settle means it
        # died (device busy / unplugged / already claimed) — which we MUST surface, else a silently
        # dead jammer reads as "the agent avoided a clean band" and fabricates a Stage-2/3 result.
        self._err_log = os.path.join(self._tmpdir, "hackrf.err")
        self._err_fh = open(self._err_log, "wb")
        self._proc = subprocess.Popen(
            ["hackrf_transfer", "-t", self._wave, "-f", str(int(self.center_freq_hz)),
             "-s", str(self.sample_rate), "-a", str(self.amp_enable),
             "-x", str(self.if_gain), "-R"],
            stdout=subprocess.DEVNULL, stderr=self._err_fh, preexec_fn=_pdeathsig,
        )
        time.sleep(self.settle_s)  # let the TX spin up before measuring
        if self._proc.poll() is not None:  # exited during settle -> launch failed
            rc = self._proc.returncode
            self._proc = None
            self._err_fh.close()
            try:
                err = Path(self._err_log).read_text(errors="replace").strip()
            except OSError:
                err = ""
            raise RuntimeError(
                f"hackrf_transfer exited with code {rc} during startup — the jammer is NOT "
                f"radiating (device busy/unplugged/claimed?). stderr: {err or '(empty)'}")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        if self._err_fh is not None:
            self._err_fh.close()
            self._err_fh = None
        time.sleep(0.4)  # let the device release

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        """Full teardown: stop the TX and remove the temp wave dir. Call this (not just stop()) when
        DISCARDING an interferer outside a ``with`` block — otherwise the gr_autopilot_itf_* tempdir
        leaks. stop() stays non-destructive so FollowerJammer.retune() can stop/start on the same dir."""
        self.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> "HackRFInterferer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FollowerJammer(HackRFInterferer):
    """Stage-3 channel-following jammer: a HackRF that CHASES the link's center frequency.

    ``hackrf_transfer`` takes its LO as a launch argument and has no live retune, so the only way to
    move a CLI-driven HackRF is to kill and relaunch it. That kill/restart cost *is* the jammer's
    reaction latency. Measured: ~1 s on the cabled bench (device release + TX settle), and 1.77 s
    driven through the daemon's jammer instrument (2026-09-07, chamber; three consecutive retunes
    at 1.774-1.776 s, the instrument's own arm/settle included). Quote the figure for the path
    being reported. Either way it is why a Pluto link, which retunes its LO *live* on a running
    flowgraph in ~4 ms, out-hops this follower: the link changes channel two to three orders of
    magnitude faster than the follower can chase.

    This is a channel-follower (retunes toward the link's *channel* over time), not a symbol-reactive
    jammer (detect-and-jam within a symbol — needs a full-duplex/FPGA radio, out of reach here).
    Frequency hopping defends against the former: hop faster than the follower can retune. The honest
    limit is unchanged — a lower-latency follower (live-retune SDR / FPGA) with reaction < the link's
    dwell wins (see ``link/hopping.py``).
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retune_count = 0
        self.last_latency_s = 0.0

    def retune(self, center_freq_hz: float) -> float:
        """Chase the link to ``center_freq_hz`` (kill/restart). Returns the reaction latency (s);
        a no-op (0.0) if already there and still transmitting."""
        f = float(center_freq_hz)
        if f == self.center_freq_hz and self.running():
            return 0.0
        t0 = time.perf_counter()
        self.stop()
        self.center_freq_hz = f
        self.start()
        self.last_latency_s = time.perf_counter() - t0
        self.retune_count += 1
        return self.last_latency_s
