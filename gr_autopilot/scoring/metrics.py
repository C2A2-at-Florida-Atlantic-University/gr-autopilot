"""Framework-owned physical-layer metrics: BER, EVM, SNR.

All functions are pure numpy and operate on arrays the framework produced or graded.
BER requires the transmitted and received bit streams to already be aligned (same length,
bit-for-bit correspondence). Aligning the receiver output to the known payload — frame
synchronization — is a separate framework-owned step (spec §7); these functions are the
final, un-gameable comparison.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class BerResult:
    n_bits: int
    n_errors: int
    ber: float


@dataclass(frozen=True)
class Metrics:
    """The scalar link-quality metrics that drive optimization (spec §7)."""

    ber: float
    n_bits: int
    n_errors: int
    evm_pct: float
    snr_db: float


def bit_errors(tx_bits: np.ndarray, rx_bits: np.ndarray) -> int:
    """Count differing bits. Raises if lengths differ (caller must align first)."""
    tx = np.asarray(tx_bits).ravel().astype(np.int8)
    rx = np.asarray(rx_bits).ravel().astype(np.int8)
    if tx.shape != rx.shape:
        raise ValueError(
            f"tx_bits ({tx.size}) and rx_bits ({rx.size}) must be aligned to equal length"
        )
    return int(np.count_nonzero(tx != rx))


def ber(tx_bits: np.ndarray, rx_bits: np.ndarray) -> BerResult:
    """Bit-error rate against the known payload."""
    n = int(np.asarray(tx_bits).size)
    e = bit_errors(tx_bits, rx_bits)
    return BerResult(n_bits=n, n_errors=e, ber=(e / n if n else 0.0))


def evm(rx_syms: np.ndarray, ref_syms: np.ndarray) -> dict:
    """Error-vector magnitude of received symbols vs. their reference points.

    Returns rms EVM as a fraction and a percent, plus peak EVM percent. Normalized by
    the reference constellation power (unit-energy constellations => reference power ~1).
    """
    rx = np.asarray(rx_syms, dtype=np.complex128).ravel()
    ref = np.asarray(ref_syms, dtype=np.complex128).ravel()
    if rx.shape != ref.shape:
        raise ValueError("rx_syms and ref_syms must have equal length")
    if rx.size == 0:
        return {"evm_rms": 0.0, "evm_pct": 0.0, "evm_max_pct": 0.0}
    err = rx - ref
    ref_power = float(np.mean(np.abs(ref) ** 2))
    if ref_power <= 0:
        raise ValueError("reference symbol power is zero")
    evm_rms = float(np.sqrt(np.mean(np.abs(err) ** 2) / ref_power))
    evm_max = float(np.max(np.abs(err)) / np.sqrt(ref_power))
    return {"evm_rms": evm_rms, "evm_pct": 100.0 * evm_rms, "evm_max_pct": 100.0 * evm_max}


def data_aided_snr_db(rx_syms: np.ndarray, ref_syms: np.ndarray) -> float:
    """Data-aided SNR estimate (dB): reference-signal power over residual-error power.

    ``ref_syms`` MUST be the known transmitted symbols (``LinkResult.ref_syms`` supplies them) for
    this to be genuinely data-aided. Passing the receiver's DECISIONS instead makes it a
    decision-directed MER, which is optimistically biased at low SNR (the residual to the nearest
    decided point understates the true error) — exactly the regime of interest, so don't."""
    rx = np.asarray(rx_syms, dtype=np.complex128).ravel()
    ref = np.asarray(ref_syms, dtype=np.complex128).ravel()
    if rx.shape != ref.shape:
        raise ValueError("rx_syms and ref_syms must have equal length")
    sig = float(np.sum(np.abs(ref) ** 2))
    noise = float(np.sum(np.abs(rx - ref) ** 2))
    if noise <= 0:
        return float("inf")
    return 10.0 * np.log10(sig / noise)


def snr_from_evm_db(evm_rms: float) -> float:
    """SNR (dB) implied by an rms EVM fraction: SNR = 1 / EVM**2."""
    if evm_rms <= 0:
        return float("inf")
    return -20.0 * np.log10(evm_rms)


def compute_metrics(
    tx_bits: np.ndarray,
    rx_bits: np.ndarray,
    rx_syms: np.ndarray,
    ref_syms: np.ndarray,
) -> Metrics:
    """Bundle BER + EVM + data-aided SNR for one aligned trial."""
    b = ber(tx_bits, rx_bits)
    e = evm(rx_syms, ref_syms)
    snr = data_aided_snr_db(rx_syms, ref_syms)
    return Metrics(
        ber=b.ber,
        n_bits=b.n_bits,
        n_errors=b.n_errors,
        evm_pct=e["evm_pct"],
        snr_db=snr,
    )
