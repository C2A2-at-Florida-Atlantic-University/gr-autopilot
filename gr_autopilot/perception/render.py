"""Render recovered symbols to scope images a vision model (or a person) can read.

Pure matplotlib (Agg), no radios. The images are the *input modality* for ``diagnose`` and for a real
vision-language model driving over MCP — the same constellation/spectrum a bench engineer would glance
at on a scope. Kept deliberately clean: fixed axes, a faint grid, a density shading so clusters vs a
smeared ring read at a glance.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


def _plt():
    """Import matplotlib LAZILY so the package (and the diagnose_signal tool without a render_path)
    stays dependency-free — matplotlib is only needed when a PNG is actually drawn (the optional
    ``viz`` extra). See pyproject [project.optional-dependencies]."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _norm(syms: np.ndarray) -> np.ndarray:
    x = np.asarray(syms, dtype=np.complex128)
    return x / (np.sqrt(np.mean(np.abs(x) ** 2)) + 1e-12)


def constellation_png(rx_syms, path, title: str = "RX constellation",
                      max_points: int = 6000, lim: float = 1.9) -> str:
    """Scatter the recovered constellation (unit-normalized) to ``path``. A faint 2-D density
    underlay makes tight clusters, a rotated grid, or a smeared ring obvious to the eye."""
    plt = _plt()
    x = _norm(rx_syms)
    if x.size > max_points:
        x = x[:: max(1, x.size // max_points)][:max_points]
    fig, ax = plt.subplots(figsize=(4.4, 4.4))
    ax.hexbin(x.real, x.imag, gridsize=44, extent=(-lim, lim, -lim, lim),
              cmap="Blues", mincnt=1, linewidths=0)
    ax.scatter(x.real, x.imag, s=5, alpha=0.25, color="#0f172a", edgecolors="none")
    ax.axhline(0, color="#64748b", lw=0.6)
    ax.axvline(0, color="#64748b", lw=0.6)
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_aspect("equal")
    ax.set_xlabel("I")
    ax.set_ylabel("Q")
    ax.set_title(title)
    ax.grid(alpha=0.15)
    fig.tight_layout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return str(path)


def spectrum_png(samples, path, sample_rate: float = 2_084_000.0,
                 title: str = "RX spectrum", nfft: int = 1 << 14) -> str:
    """Plot the received PSD (dB, peak-normalized) vs baseband offset (kHz) to ``path``. A jammer
    shows as a sharp off-center spur; a clean link is flat noise with the signal shoulder around DC."""
    plt = _plt()
    x = np.asarray(samples, dtype=np.complex128)
    x = x[:nfft]
    X = np.fft.fftshift(np.fft.fft(x, nfft))
    psd = 20.0 * np.log10(np.abs(X) + 1e-9)
    psd -= psd.max()
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / sample_rate)) / 1e3
    fig, ax = plt.subplots(figsize=(6.2, 3.0))
    ax.plot(f, psd, color="#2563eb", lw=0.7)
    ax.fill_between(f, psd, psd.min(), color="#93c5fd", alpha=0.25)
    ax.set_xlabel("baseband offset (kHz)")
    ax.set_ylabel("power (dB)")
    ax.set_title(title)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return str(path)
