"""Known-payload generation (framework-owned ground truth).

Uses a maximal-length linear-feedback shift register (PRBS) so the payload is:
  * deterministic and reproducible from (order, seed),
  * spectrally white and balanced (equal-ish 0/1 counts) like a real test sequence,
  * long-period (2**order - 1) so it does not repeat within a capture.

Pure numpy; no GNU Radio dependency.
"""
from __future__ import annotations

import numpy as np

# Feedback tap positions (1-indexed from the MSB) that yield a maximal-length sequence.
# Standard PRBS polynomials.
_MAXLEN_TAPS: dict[int, tuple[int, ...]] = {
    7: (7, 6),
    9: (9, 5),
    11: (11, 9),
    15: (15, 14),
    20: (20, 3),
    23: (23, 18),
}


def prbs(order: int = 15, n_bits: int = 1024, seed: int = 1) -> np.ndarray:
    """Generate ``n_bits`` bits from a maximal-length LFSR.

    Args:
        order: LFSR length; period is ``2**order - 1``. Must be a supported order.
        n_bits: number of output bits.
        seed: nonzero initial register state (masked to ``order`` bits).

    Returns:
        int8 array of 0/1 bits, length ``n_bits``.
    """
    if order not in _MAXLEN_TAPS:
        raise ValueError(f"order {order} unsupported; choose from {sorted(_MAXLEN_TAPS)}")
    if n_bits < 0:
        raise ValueError("n_bits must be non-negative")
    mask = (1 << order) - 1
    state = (seed & mask) or 1  # never allow the all-zero (lock-up) state
    taps = _MAXLEN_TAPS[order]

    out = np.empty(n_bits, dtype=np.int8)
    for i in range(n_bits):
        # Output the MSB, then clock the Fibonacci LFSR.
        out[i] = (state >> (order - 1)) & 1
        feedback = 0
        for t in taps:
            feedback ^= (state >> (t - 1)) & 1
        state = ((state << 1) | feedback) & mask
    return out


def known_payload(n_bits: int, order: int = 15, seed: int = 1) -> np.ndarray:
    """Return the canonical known payload of ``n_bits`` bits (a PRBS)."""
    return prbs(order=order, n_bits=n_bits, seed=seed)
