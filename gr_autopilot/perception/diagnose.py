"""Diagnose a recovered constellation/spectrum — turn the *picture* into a fault + a fix.

Two diagnosers behind one interface. ``HeuristicVision`` reads interpretable features off the
constellation (rotation off the grid, grid structure, angular uniformity, a spectral spur) and maps
them to a named fault and the MCP action that fixes it — fast, deterministic, no radios, and unit
tested. A real **vision-language model** driving over MCP implements the same ``diagnose`` signature
on the rendered PNG (``perception.render``); the framework hands it the same scope image a bench
engineer would read. Either way the diagnosis is derived from the *received* signal the agent already
has — it never sees the hidden channel SNR.

The five faults the agent must tell apart, and why the picture disambiguates them where a scalar BER
cannot: a **phase offset** is a *rotated but tight* grid (fix the carrier, don't drop the rate); an
**unlocked carrier** is a smeared ring (the 16-QAM-on-a-Costas-loop failure); **low SNR** is a fuzzy
cloud with no structure (drop the rate / add coding); **interference** is a spur off-center in the
spectrum (retune away); **clean** is a tight aligned grid (climb).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gr_autopilot import modulation as _modulation

# 16-QAM grid, unit average energy — the default reference when no modulation is supplied.
_GRID16 = np.array([a + 1j * b for a in (-3, -1, 1, 3) for b in (-3, -1, 1, 3)]) / np.sqrt(10.0)


def _reference_grid(modulation: str | None):
    """Unit-energy constellation points for ``modulation`` (the grid the rx is scored against).
    Falls back to the 16-QAM grid when the modulation is unknown/None, preserving old behaviour."""
    if modulation is None:
        return _GRID16
    try:
        return _modulation.get(modulation).points
    except (ValueError, KeyError):
        return _GRID16


def _symmetry_order(modulation: str | None) -> int:
    """Rotational symmetry order of the constellation: BPSK is 2-fold (points on one axis), 8-PSK
    is 8-fold, QPSK and every QAM are 4-fold. Sets which moment (x^k) locks the grid orientation."""
    m = (modulation or "").lower()
    if m == "bpsk":
        return 2
    if m in ("8psk", "psk8"):
        return 8
    return 4


def _clean_evm_for(modulation: str | None, frac: float) -> float:
    """The de-rotated EVM below which a cloud counts as tight, as a FRACTION of the constellation's
    minimum decision distance. An absolute threshold cannot be right for more than one modulation:
    the same 14% EVM is a comfortable BPSK link and a failing 64-QAM one. ``frac`` 0.22 reproduces
    the previous absolute 0.14 for 16-QAM (d_min 0.632) and gives BPSK 0.44, QPSK 0.31, 8-PSK 0.17,
    64-QAM 0.07."""
    try:
        return frac * _modulation.min_distance(modulation or "16qam")
    except (ValueError, KeyError):
        return frac * _modulation.min_distance("16qam")


@dataclass
class Diagnosis:
    fault: str                       # clean | phase_offset | carrier_unlocked | low_snr | interference
    summary: str                     # one line a human/LLM reads
    action: str                      # the MCP action that addresses it
    confidence: float                # 0..1
    features: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"fault": self.fault, "summary": self.summary, "action": self.action,
                "confidence": round(float(self.confidence), 2),
                "features": {k: round(float(v), 4) for k, v in self.features.items()}}


def constellation_features(rx_syms, modulation: str | None = "16qam") -> dict:
    """Interpretable features of a recovered constellation (unit-normalized), scored against the
    grid for ``modulation`` (defaults to 16-QAM). Passing the *actual* modulation is what keeps a
    clean BPSK/QPSK link from being mis-scored against a 16-QAM grid and mislabeled low-SNR."""
    grid = _reference_grid(modulation)
    k = _symmetry_order(modulation)
    # Axis-aligned QAM/QPSK has E[x^4] at angle pi (points sit at odd multiples of 45 degrees);
    # axis-aligned BPSK has E[x^2] at angle 0, and 8-PSK -- a point on the positive real axis --
    # has E[x^8] at angle 0 as well.
    ref_angle = 0.0 if k in (2, 8) else np.pi
    x = np.asarray(rx_syms, dtype=np.complex128)
    x = x[np.isfinite(x)]
    if x.size < 16:
        return {"rotation_deg": 0.0, "grid_structure": 0.0, "derot_evm": 1.0, "ang_uniformity": 1.0}
    x = x / (np.sqrt(np.mean(np.abs(x) ** 2)) + 1e-12)
    # k-th-power moment: the constellation's k-fold symmetry makes E[x^k] lock to the grid
    # orientation. Its PHASE gives the rotation off-grid; its (normalized) MAGNITUDE gives how much
    # grid structure survives — ~0 for a spinning ring or pure noise, high for a locked constellation.
    xk = x ** k
    mk = np.mean(xk)
    grid_structure = float(np.abs(mk) / (np.mean(np.abs(xk)) + 1e-12))
    # Measure the off-grid rotation RELATIVE to the axis-aligned reference angle; angle()/k then
    # lands in (-180/k, 180/k] by the grid's k-fold symmetry.
    rotation_deg = float(np.degrees(np.angle(mk * np.exp(-1j * ref_angle)) / k))
    # EVM after de-rotating onto the grid: low => it really is a (rotated) grid, not just noise.
    xr = x * np.exp(-1j * np.radians(rotation_deg))
    d = np.min(np.abs(xr[:, None] - grid[None, :]), axis=1)
    derot_evm = float(np.sqrt(np.mean(d ** 2)))
    # angular uniformity (entropy of the phase histogram): ~1 => angles uniform => a spinning ring.
    ang = np.angle(x)
    hist, _ = np.histogram(ang, bins=24, range=(-np.pi, np.pi))
    p = hist / (hist.sum() + 1e-12)
    ang_uniformity = float(-(p * np.log(p + 1e-12)).sum() / np.log(len(p)))
    return {"rotation_deg": rotation_deg, "grid_structure": grid_structure,
            "derot_evm": derot_evm, "ang_uniformity": ang_uniformity}


def spectrum_spur(samples, sample_rate: float = 2_084_000.0, sig_bw_hz: float = 420_000.0,
                  nfft: int = 1 << 14) -> dict:
    """Look for a narrowband interferer OUTSIDE the signal band: the largest out-of-band PSD peak and
    its prominence over the noise floor. A strong off-center spur is a jammer to retune away from."""
    x = np.asarray(samples, dtype=np.complex128)[:nfft]
    if x.size < 256:
        return {"spur_offset_khz": 0.0, "spur_prominence_db": 0.0}
    psd = np.abs(np.fft.fftshift(np.fft.fft(x, nfft))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / sample_rate))
    floor = float(np.median(psd)) + 1e-12
    out = np.abs(f) > sig_bw_hz / 2.0
    if not out.any():
        return {"spur_offset_khz": 0.0, "spur_prominence_db": 0.0}
    k = int(np.argmax(np.where(out, psd, 0.0)))
    return {"spur_offset_khz": float(f[k] / 1e3),
            "spur_prominence_db": float(10.0 * np.log10(psd[k] / floor))}


class HeuristicVision:
    """Fast, deterministic diagnoser from constellation (+ optional spectrum) features."""

    def __init__(self, evm_clean: float | None = None, rot_thresh_deg: float = 8.0,
                 ring_uniformity: float = 0.94, spur_db: float = 10.0,
                 evm_clean_frac: float = 0.22):
        # ``evm_clean`` (absolute) is kept for callers that pin it; by default the threshold is
        # relative to the modulation's decision distance (see _clean_evm_for).
        self.evm_clean = evm_clean
        self.evm_clean_frac = evm_clean_frac
        self.rot_thresh_deg = rot_thresh_deg
        self.ring_uniformity = ring_uniformity
        self.spur_db = spur_db

    def diagnose(self, rx_syms, samples=None, sample_rate: float = 2_084_000.0,
                 modulation: str | None = "16qam") -> Diagnosis:
        f = constellation_features(rx_syms, modulation=modulation)
        spur = spectrum_spur(samples, sample_rate) if samples is not None else \
            {"spur_offset_khz": 0.0, "spur_prominence_db": 0.0}
        f = {**f, **spur}
        rot, gs, evm, uni = (f["rotation_deg"], f["grid_structure"], f["derot_evm"],
                             f["ang_uniformity"])

        # 1) interference first: a strong off-center spur is a jammer regardless of the constellation.
        if spur["spur_prominence_db"] >= self.spur_db and abs(spur["spur_offset_khz"]) > 1.0:
            off = spur["spur_offset_khz"]
            return Diagnosis("interference",
                             f"narrowband interferer ~{off:+.0f} kHz off center "
                             f"(+{spur['spur_prominence_db']:.0f} dB) — the constellation is being jammed",
                             "sense_spectrum the candidates and set_center_freq to a clear channel",
                             confidence=0.9, features=f)
        # "Tight" is judged against THIS modulation's decision distance, not a fixed number.
        clean_thr = self.evm_clean if self.evm_clean is not None else \
            _clean_evm_for(modulation, self.evm_clean_frac)
        f["evm_clean_threshold"] = clean_thr
        # 2) unlocked carrier: uniform angles + little grid structure + NO grid fit => a spinning
        #    ring. The last condition matters: the 32-QAM cross has a weak 4th-power moment even
        #    when perfectly locked, so moment strength alone would call a clean cross a ring.
        if uni >= self.ring_uniformity and gs < 0.25 and evm > clean_thr:
            return Diagnosis("carrier_unlocked",
                             "constellation is a smeared ring — the carrier is not locked "
                             "(QAM on a constant-modulus loop spins)",
                             "use the ZC/DD (or PSAM) QAM path, or drop to a PSK rung",
                             confidence=0.8, features=f)
        # 3) tight grid after de-rotation => structured. Rotated => phase offset; aligned => clean.
        if evm <= clean_thr:
            if abs(rot) >= self.rot_thresh_deg:
                return Diagnosis("phase_offset",
                                 f"tight constellation rotated ~{rot:+.0f}° off the grid — a constant "
                                 f"carrier phase offset, not low SNR",
                                 "start_bo_run(['phase_correction_rad']) to tune the carrier phase",
                                 confidence=0.85, features=f)
            return Diagnosis("clean",
                             "tight, grid-aligned constellation — the link is healthy",
                             "climb the modulation ladder for spectral efficiency",
                             confidence=0.9, features=f)
        # 4) otherwise: a fuzzy cloud with no recoverable structure => low SNR.
        return Diagnosis("low_snr",
                         "fuzzy cloud, no tight clusters even after de-rotation — the link is SNR-limited",
                         "drop the modulation ladder or add coding (start_bo_run won't rescue noise)",
                         confidence=0.7, features=f)


def diagnose_constellation(rx_syms, samples=None, sample_rate: float = 2_084_000.0,
                           modulation: str | None = "16qam") -> Diagnosis:
    """Convenience: diagnose with the default heuristic vision. Pass the link's ``modulation`` so the
    constellation is scored against the right grid (a clean BPSK/QPSK link is not called low-SNR)."""
    return HeuristicVision().diagnose(rx_syms, samples=samples, sample_rate=sample_rate,
                                      modulation=modulation)
