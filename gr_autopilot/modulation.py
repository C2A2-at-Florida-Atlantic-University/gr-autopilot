"""Gray-coded digital modulations: BPSK, QPSK, 8-PSK, 16/32/64/256-QAM.

Single source of truth for the constellation used by the transmitter (bit->symbol),
the receiver (symbol->bit hard decision), and the scoring layer's EVM reference. All
constellations are normalized to unit average symbol energy (Es = 1) so that a given
Es/N0 maps to closed-form BER theory without hidden scale factors.

Square QAM is built from a Gray-coded PAM per axis (I from the high bits, Q from the low), so
every nearest neighbour differs in exactly one bit. 32-QAM is the standard cross constellation
(a 6x6 grid with the corners removed), Gray-coded as far as a cross allows: all in-grid
neighbours differ by one bit; a handful of pairs across the removed corners differ by two.
M-PSK is Gray-coded around the circle.

Which of these a real radio can carry is a measurement, not a property of this module: the
bench's tracking receiver and its error-vector floor decide. See docs/wiki/flowgraphs.md.

Pure numpy; no GNU Radio dependency.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Gray-coded 4-PAM: 2 bits (b_hi, b_lo) -> amplitude level, adjacent levels differ by 1 bit.
_GRAY_4PAM = {(0, 0): -3.0, (0, 1): -1.0, (1, 1): 1.0, (1, 0): 3.0}


def _gray(n: int) -> int:
    return n ^ (n >> 1)


def _gray_pam(bits_per_axis: int) -> list[float]:
    """Levels indexed by the Gray code of the level number: adjacent levels differ in one bit.
    ``bits_per_axis=2`` reproduces ``_GRAY_4PAM``."""
    m = 1 << bits_per_axis
    levels = [float(2 * i - (m - 1)) for i in range(m)]          # -(m-1) .. +(m-1) step 2
    out = [0.0] * m
    for i, lvl in enumerate(levels):
        out[_gray(i)] = lvl
    return out


def _square_qam(bits_per_symbol: int) -> dict:
    """Gray square QAM: the high half of the bits select the I level, the low half the Q level."""
    half = bits_per_symbol // 2
    pam = _gray_pam(half)
    pts = {}
    for idx in range(1 << bits_per_symbol):
        hi, lo = idx >> half, idx & ((1 << half) - 1)
        pts[idx] = complex(pam[hi], pam[lo])
    return pts


def _cross_32qam() -> dict:
    """32-QAM cross: a 6x6 grid minus its four corners, 5 bits.

    Layout follows the common construction: the two high bits pick a quadrant, the three low bits
    pick one of eight points inside it (a 2x4 block plus the two 'wing' points), Gray-coded within
    the block so in-grid neighbours differ by one bit."""
    # eight points of the first quadrant (I>0, Q>0), Gray-ordered by the three low bits
    quad = {
        0b000: (1, 1), 0b001: (1, 3), 0b011: (1, 5), 0b010: (3, 1),
        0b110: (3, 3), 0b111: (3, 5), 0b101: (5, 1), 0b100: (5, 3),
    }
    sign = {0b00: (1, 1), 0b01: (-1, 1), 0b11: (-1, -1), 0b10: (1, -1)}   # Gray around quadrants
    pts = {}
    for idx in range(32):
        q, low = idx >> 3, idx & 0b111
        si, sq = sign[q]
        i, qq = quad[low]
        pts[idx] = complex(si * i, sq * qq)
    return pts


def _mpsk(bits_per_symbol: int) -> dict:
    """Gray M-PSK: point k sits at angle 2*pi*gray^-1(k)/M -- neighbours differ by one bit."""
    m = 1 << bits_per_symbol
    pts = {}
    for i in range(m):
        pts[_gray(i)] = complex(np.exp(1j * (2 * np.pi * i / m + (np.pi / 4 if m == 4 else 0.0))))
    return pts


_ALIASES = {"qam16": "16qam", "qam32": "32qam", "qam64": "64qam", "qam256": "256qam", "psk8": "8psk"}


def _build_points(mod: str) -> np.ndarray:
    """Return complex points indexed by the bit-integer they encode (bit 0 is the MSB).

    points[idx] is the constellation point for the ``bits_per_symbol``-bit integer ``idx``.
    """
    mod = _ALIASES.get(mod.lower(), mod.lower())
    if mod == "bpsk":
        pts = {0: -1.0 + 0j, 1: 1.0 + 0j}
    elif mod == "qpsk":
        pts = {}
        for b0 in (0, 1):
            for b1 in (0, 1):
                idx = (b0 << 1) | b1
                pts[idx] = complex(1 - 2 * b0, 1 - 2 * b1)
    elif mod == "8psk":
        pts = _mpsk(3)
    elif mod == "16qam":
        pts = _square_qam(4)
    elif mod == "32qam":
        pts = _cross_32qam()
    elif mod == "64qam":
        pts = _square_qam(6)
    elif mod == "256qam":
        pts = _square_qam(8)
    else:
        raise ValueError(f"unknown modulation {mod!r} (use one of {', '.join(MODULATIONS)})")
    points = np.array([pts[i] for i in range(len(pts))], dtype=np.complex128)
    # Normalize to unit average energy; every closed form below assumes Es = 1.
    points = points / np.sqrt(float(np.mean(np.abs(points) ** 2)))
    es = float(np.mean(np.abs(points) ** 2))
    if not np.isclose(es, 1.0, atol=1e-9):
        raise AssertionError(f"{mod}: average symbol energy {es} != 1")
    return points


@dataclass(frozen=True)
class Constellation:
    """A Gray-coded, unit-energy constellation with modulate/demodulate."""

    name: str
    points: np.ndarray  # complex128, indexed by bit-integer

    @property
    def bits_per_symbol(self) -> int:
        return int(np.log2(len(self.points)))

    @property
    def order(self) -> int:
        return len(self.points)

    def modulate(self, bits: np.ndarray) -> np.ndarray:
        """Map a 1-D bit array (length a multiple of bits_per_symbol) to symbols."""
        bits = np.asarray(bits, dtype=np.int64).ravel()
        bps = self.bits_per_symbol
        if bits.size % bps != 0:
            raise ValueError(f"bit count {bits.size} not a multiple of {bps}")
        groups = bits.reshape(-1, bps)
        weights = (1 << np.arange(bps - 1, -1, -1)).astype(np.int64)  # MSB first
        idx = groups @ weights
        return self.points[idx]

    def hard_decision(self, syms: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Nearest-point decision.

        Returns ``(rx_bits, ref_points)`` where ``ref_points`` are the decided ideal
        constellation points (for EVM against the *decisions*).
        """
        syms = np.asarray(syms, dtype=np.complex128).ravel()
        # Distance from each rx symbol to every constellation point.
        dist = np.abs(syms[:, None] - self.points[None, :])
        idx = np.argmin(dist, axis=1)
        ref = self.points[idx]
        bps = self.bits_per_symbol
        # idx -> bits (MSB first)
        shifts = np.arange(bps - 1, -1, -1)
        bits = ((idx[:, None] >> shifts) & 1).astype(np.int8).ravel()
        return bits, ref

    def soft_bits(self, syms: np.ndarray) -> np.ndarray:
        """Max-log per-bit LLRs (same MSB-first order as ``hard_decision``), for soft-decision FEC.

        ``LLR = min_{s: bit=1} |y-s|^2 - min_{s: bit=0} |y-s|^2`` (unnormalized — the ``1/sigma^2``
        scale is a per-run constant that does not change the Viterbi argmin). ``LLR > 0`` means bit 0
        is the more likely value, so ``sign`` agrees with the hard decision but the magnitude carries
        the reliability the soft decoder exploits for ~2 dB of extra coding gain.
        """
        syms = np.asarray(syms, dtype=np.complex128).ravel()
        bps = self.bits_per_symbol
        d2 = np.abs(syms[:, None] - self.points[None, :]) ** 2      # (n_syms, order)
        pt_idx = np.arange(self.order)
        llr = np.empty((syms.size, bps), dtype=np.float64)
        for j in range(bps):
            bit_j = (pt_idx >> (bps - 1 - j)) & 1                   # bit j of each constellation point
            llr[:, j] = d2[:, bit_j == 1].min(axis=1) - d2[:, bit_j == 0].min(axis=1)
        return llr.ravel()


_CACHE: dict[str, Constellation] = {}

# In ladder order: ascending bits per symbol. The AMC controllers climb this.
MODULATIONS = ("bpsk", "qpsk", "8psk", "16qam", "32qam", "64qam", "256qam")


def get(mod: str) -> Constellation:
    """Return the (cached) Constellation for a modulation name (aliases such as qam64 accepted)."""
    key = _ALIASES.get(mod.lower(), mod.lower())
    if key not in _CACHE:
        _CACHE[key] = Constellation(name=key, points=_build_points(key))
    return _CACHE[key]


def bits_per_symbol(mod: str) -> int:
    return get(mod).bits_per_symbol


def canonical(mod: str) -> str:
    """The canonical spelling of a modulation name (``qam64`` -> ``64qam``)."""
    key = _ALIASES.get(mod.lower(), mod.lower())
    if key not in MODULATIONS:
        raise ValueError(f"unknown modulation {mod!r} (use one of {', '.join(MODULATIONS)})")
    return key


def min_distance(mod: str) -> float:
    """Smallest distance between two points of the unit-energy constellation: the scale every
    'is this cloud tight?' question has to be asked against, since 14% EVM is harmless for BPSK
    (d_min = 2) and fatal for 256-QAM (d_min ~ 0.15)."""
    pts = get(mod).points
    d = np.abs(pts[:, None] - pts[None, :])
    return float(d[d > 1e-12].min())
