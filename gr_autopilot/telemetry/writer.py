"""Telemetry snapshot writer: turn a graded trial into the JSON the dashboard renders.

The running loop calls ``write(...)`` after each trial; the server reads the latest snapshot
(plus the edit ledger) and the page polls it. Writes are atomic (temp file + rename) so the
server never reads a half-written snapshot.

Each snapshot carries a wall-clock ``ts``. The snapshot file is a LAST-VALUE store — it is
overwritten per trial and never cleared — so without a timestamp a stopped run is
indistinguishable from a live one and the dashboard would keep presenting the final frame as
current. ``ts`` is what lets ``GET /data`` age the snapshot and the page fall back to IDLE.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np


def _fin(x):
    """Coerce a non-finite float (e.g. SNR=inf on a zero-error link) to None so /data stays valid
    JSON — a strict JSON.parse in the dashboard rejects bare Infinity/NaN."""
    x = float(x)
    return round(x, 2) if math.isfinite(x) else None

from gr_autopilot.link.backend import LinkResult
from gr_autopilot.scoring.metrics import Metrics


def _downsample(arr: np.ndarray, n: int) -> np.ndarray:
    arr = np.asarray(arr)
    if arr.size <= n:
        return arr
    idx = np.linspace(0, arr.size - 1, n).astype(int)
    return arr[idx]


def constellation_points(rx_syms: np.ndarray, n: int = 400) -> list[list[float]]:
    """Up to ``n`` received symbols, normalized to unit average power, as [I, Q] pairs."""
    s = _downsample(np.asarray(rx_syms, dtype=np.complex128).ravel(), n)
    if s.size == 0:
        return []
    p = np.sqrt(np.mean(np.abs(s) ** 2)) + 1e-12
    s = s / p
    return [[round(float(z.real), 4), round(float(z.imag), 4)] for z in s]


def spectrum_db(samples: np.ndarray, n: int = 160, span_hz: float | None = None,
                center_hz: float | None = None) -> dict:
    """Normalized power spectral density (peak = 0 dB) over normalized frequency [-0.5, 0.5].

    ``span_hz`` is the full width the [-0.5, 0.5] axis represents and ``center_hz`` the radio
    frequency it is centred on; both are carried through so the console can label the axis in
    absolute Hz instead of leaving the reader to interpret a bare 0. They are metadata only --
    the ``freqs`` array stays normalized, so an older page renders unchanged.

    NOTE on what ``span_hz`` means here: this transform is fed the RECOVERED SYMBOLS unless a raw
    capture is supplied, and those are one sample per symbol, so the axis spans the SYMBOL RATE,
    not the radio's sample rate, and the pulse shaping has already been removed by the matched
    filter. The span is reported so the axis can be honest about which it is, not to imply this
    is an RF spectrum analyser trace.
    """
    x = np.asarray(samples, dtype=np.complex128).ravel()
    if x.size < 16:
        return {"freqs": [], "psd_db": []}
    w = np.hanning(x.size)
    X = np.fft.fftshift(np.fft.fft(x * w))
    psd = 20.0 * np.log10(np.abs(X) + 1e-9)
    psd -= float(psd.max())
    freqs = np.fft.fftshift(np.fft.fftfreq(x.size))
    fi, pi = _downsample(freqs, n), _downsample(psd, n)
    out = {"freqs": [round(float(f), 4) for f in fi],
           "psd_db": [round(float(max(p, -80.0)), 2) for p in pi]}
    if span_hz:
        out["span_hz"] = float(span_hz)
    if center_hz:
        out["center_hz"] = float(center_hz)
    return out


class TelemetryWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._t = 0

    def write(self, *, result: LinkResult, metrics: Metrics, target_ber: float,
              structure: dict, active_loop: dict | None = None,
              rx_capture: np.ndarray | None = None, goal: str = "",
              occupancy: list | None = None, tokens: dict | None = None,
              control: dict | None = None, diagnosis: dict | None = None,
              span_hz: float | None = None, center_hz: float | None = None) -> dict:
        """Write one snapshot. ``rx_capture`` (raw samples) gives a true spectrum on hardware;
        without it the spectrum falls back to the FFT of the recovered symbols. ``occupancy`` is
        the monitor's energy-detection sweep (``backend.sense_spectrum`` rows) when the agent is
        sensing the band; omitted from the snapshot when there is none (Stage-1 clean link)."""
        self._t += 1
        spec_src = rx_capture if rx_capture is not None else result.rx_syms
        snap = {
            "t": self._t,
            "ts": time.time(),   # wall clock; GET /data ages this so a stopped run reads IDLE
            "goal": goal,
            "structure": structure,
            "metrics": {
                "ber": _fin(metrics.ber), "evm_pct": _fin(metrics.evm_pct),
                "snr_db": _fin(metrics.snr_db), "target_ber": float(target_ber),
                "feasible": bool(metrics.ber <= target_ber),
            },
            "active_loop": active_loop or {"loop": "outer", "detail": ""},
            "constellation": constellation_points(result.rx_syms),
            "spectrum": spectrum_db(spec_src, span_hz=span_hz, center_hz=center_hz),
        }
        if occupancy:
            snap["occupancy"] = [
                {"center_freq_hz": float(o["center_freq_hz"]),
                 "power_db": round(float(o["power_db"]), 2),
                 "occupied": bool(o["occupied"]),
                 # carry the producing backend's detection margin so the dashboard threshold line
                 # matches its occupied decision (sim 3 dB vs Pluto 6 dB), not a hardcoded 3 dB.
                 "detect_margin_db": float(o.get("detect_margin_db", 3.0))}
                for o in occupancy
            ]
        if tokens:  # MCP tool-surface token footprint (see StdioMCPServer.token_stats)
            snap["tokens"] = tokens
        if control:  # current operator-console settings, so the dashboard controls reflect state
            # NEVER serialize the operator's hidden channel setpoint (es_n0_db) into the network
            # snapshot served at GET /data — keep only agent-legal display state.
            snap["control"] = {k: v for k, v in control.items() if k != "es_n0_db"}
        if diagnosis:  # the agent's vision read of the constellation (perception.diagnose_signal)
            snap["diagnosis"] = diagnosis
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, allow_nan=False))   # reject any stray non-finite value
        tmp.replace(self.path)  # atomic swap
        return snap
