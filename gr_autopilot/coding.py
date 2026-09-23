"""Forward error correction: K=3, rate-1/2 convolutional code + hard-decision Viterbi.

The "C" in AMC (adaptive modulation *and coding*): a rate-adaptive FEC the agent can trade
against modulation order. This is the smallest useful code — constraint length 3 (4 trellis
states), generators (7, 5) octal — giving ~3-4 dB of hard-decision coding gain, in pure numpy
so it has no dependency and round-trips exactly. The encoder flushes the register with K-1 zero
bits so decoding always terminates in the zero state.
"""
from __future__ import annotations

import numpy as np

_K = 3                       # constraint length
_MEM = _K - 1                # memory bits (2 -> 4 states)
_G = (0b111, 0b101)          # generator polynomials (7, 5) octal
_NSTATES = 1 << _MEM
CODE_RATE = 0.5


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


# Precomputed trellis: for (state, input) -> (next_state, 2 output bits packed as [o0, o1]).
def _build_trellis():
    nxt = np.zeros((_NSTATES, 2), dtype=np.int64)
    out = np.zeros((_NSTATES, 2, 2), dtype=np.int8)
    for s in range(_NSTATES):
        for b in (0, 1):
            window = (b << _MEM) | s            # [b_t, b_{t-1}, b_{t-2}]
            out[s, b, 0] = _parity(window & _G[0])
            out[s, b, 1] = _parity(window & _G[1])
            nxt[s, b] = (b << (_MEM - 1)) | (s >> 1)   # new memory = [b_t, b_{t-1}]
    return nxt, out


_NXT, _OUT = _build_trellis()


def conv_encode(bits: np.ndarray) -> np.ndarray:
    """Encode info bits -> 2x coded bits (with a K-1 zero-bit tail flush)."""
    bits = np.asarray(bits, dtype=np.int64).ravel()
    s = 0
    out = np.empty((bits.size + _MEM) * 2, dtype=np.int8)
    j = 0
    for b in list(bits) + [0] * _MEM:            # tail-flush to state 0
        out[j] = _OUT[s, b, 0]
        out[j + 1] = _OUT[s, b, 1]
        s = _NXT[s, b]
        j += 2
    return out


def viterbi_decode(coded: np.ndarray, n_info: int) -> np.ndarray:
    """Hard-decision Viterbi. ``coded`` is (n_info + MEM)*2 bits; returns n_info info bits. A
    coded value of -1 is an erasure (punctured position) and is skipped in the branch metric."""
    coded = np.asarray(coded, dtype=np.int64).ravel()
    steps = coded.size // 2
    INF = 1 << 30
    pm = np.full(_NSTATES, INF, dtype=np.int64)
    pm[0] = 0                                    # known start state
    back = np.zeros((steps, _NSTATES), dtype=np.int8)     # chosen input per (step, next_state)
    prev = np.zeros((steps, _NSTATES), dtype=np.int64)    # predecessor state

    for t in range(steps):
        r0, r1 = coded[2 * t], coded[2 * t + 1]
        npm = np.full(_NSTATES, INF, dtype=np.int64)
        for s in range(_NSTATES):
            if pm[s] >= INF:
                continue
            for b in (0, 1):
                ns = _NXT[s, b]
                bm = (0 if r0 < 0 else (_OUT[s, b, 0] ^ r0)) + \
                     (0 if r1 < 0 else (_OUT[s, b, 1] ^ r1))       # Hamming (skip erasures)
                cand = pm[s] + bm
                if cand < npm[ns]:
                    npm[ns] = cand
                    back[t, ns] = b
                    prev[t, ns] = s
        pm = npm

    # Traceback from the (flushed) zero state.
    s = 0
    bits_rev = np.empty(steps, dtype=np.int8)
    for t in range(steps - 1, -1, -1):
        bits_rev[t] = back[t, s]
        s = prev[t, s]
    return bits_rev[:n_info]


def viterbi_decode_soft(llr: np.ndarray, n_info: int) -> np.ndarray:
    """Soft-decision Viterbi. ``llr`` is (n_info + MEM)*2 per-coded-bit LLRs (``> 0`` favours bit 0,
    magnitude = reliability; a 0 is an erasure). Uses the correlation branch metric
    ``sum_i llr_i * (2*c_i - 1)`` (minimize) instead of Hamming distance, which buys ~2 dB over the
    hard-decision decoder by exploiting how *confident* each demodulated bit was."""
    llr = np.asarray(llr, dtype=np.float64).ravel()
    steps = llr.size // 2
    INF = 1e18
    pm = np.full(_NSTATES, INF, dtype=np.float64)
    pm[0] = 0.0                                  # known start state
    back = np.zeros((steps, _NSTATES), dtype=np.int8)
    prev = np.zeros((steps, _NSTATES), dtype=np.int64)

    for t in range(steps):
        l0, l1 = llr[2 * t], llr[2 * t + 1]
        npm = np.full(_NSTATES, INF, dtype=np.float64)
        for s in range(_NSTATES):
            if pm[s] >= INF:
                continue
            for b in (0, 1):
                ns = _NXT[s, b]
                bm = l0 * (2 * _OUT[s, b, 0] - 1) + l1 * (2 * _OUT[s, b, 1] - 1)  # soft correlation
                cand = pm[s] + bm
                if cand < npm[ns]:
                    npm[ns] = cand
                    back[t, ns] = b
                    prev[t, ns] = s
        pm = npm

    s = 0
    bits_rev = np.empty(steps, dtype=np.int8)
    for t in range(steps - 1, -1, -1):
        bits_rev[t] = back[t, s]
        s = prev[t, s]
    return bits_rev[:n_info]


# --- Puncturing: turn the rate-1/2 mother code into rate 3/4 (keep 4 of every 6 output bits) ---
_PUNCT_R34 = np.array([[1, 1, 0], [1, 0, 1]], dtype=np.int8)   # [generator, input-in-period-3]
CODING_RATES = {"conv_k3_r12": 0.5, "conv_k3_r34": 0.75}


def puncture(coded: np.ndarray, pattern: np.ndarray = _PUNCT_R34) -> np.ndarray:
    period = pattern.shape[1]
    n_inputs = coded.size // 2
    keep = []
    for i in range(n_inputs):
        p = pattern[:, i % period]
        if p[0]:
            keep.append(coded[2 * i])
        if p[1]:
            keep.append(coded[2 * i + 1])
    return np.array(keep, dtype=np.int8)


def depuncture(recv: np.ndarray, n_inputs: int, pattern: np.ndarray = _PUNCT_R34) -> np.ndarray:
    """Reinsert erasures (-1) at the punctured positions to rebuild a rate-1/2 stream."""
    recv = np.asarray(recv).ravel()
    period = pattern.shape[1]
    out = np.full(n_inputs * 2, -1, dtype=np.int8)
    j = 0
    for i in range(n_inputs):
        p = pattern[:, i % period]
        if p[0] and j < recv.size:
            out[2 * i] = recv[j]; j += 1
        if p[1] and j < recv.size:
            out[2 * i + 1] = recv[j]; j += 1
    return out


def depuncture_soft(recv_llr: np.ndarray, n_inputs: int, pattern: np.ndarray = _PUNCT_R34) -> np.ndarray:
    """Reinsert 0-LLR erasures (no information) at the punctured positions to rebuild a rate-1/2 stream."""
    recv = np.asarray(recv_llr, dtype=np.float64).ravel()
    period = pattern.shape[1]
    out = np.zeros(n_inputs * 2, dtype=np.float64)     # 0 = erasure (soft: no information)
    j = 0
    for i in range(n_inputs):
        p = pattern[:, i % period]
        if p[0] and j < recv.size:
            out[2 * i] = recv[j]; j += 1
        if p[1] and j < recv.size:
            out[2 * i + 1] = recv[j]; j += 1
    return out


def coding_encode(info_bits: np.ndarray, coding: str) -> np.ndarray:
    """Encode info bits under a named code ("conv_k3_r12" | "conv_k3_r34")."""
    coded = conv_encode(info_bits)
    if coding == "conv_k3_r12":
        return coded
    if coding == "conv_k3_r34":
        return puncture(coded)
    raise ValueError(f"unknown coding {coding!r}")


def coding_decode(demod_bits: np.ndarray, coding: str, n_info: int) -> np.ndarray:
    """Decode demodulated (hard) coded bits back to n_info info bits."""
    if coding == "conv_k3_r12":
        return viterbi_decode(demod_bits, n_info)
    if coding == "conv_k3_r34":
        return viterbi_decode(depuncture(demod_bits, n_info + _MEM), n_info)
    raise ValueError(f"unknown coding {coding!r}")


def coding_decode_soft(llr: np.ndarray, coding: str, n_info: int) -> np.ndarray:
    """Soft-decision decode from per-coded-bit LLRs (see ``Constellation.soft_bits``) -> n_info bits."""
    if coding == "conv_k3_r12":
        return viterbi_decode_soft(llr, n_info)
    if coding == "conv_k3_r34":
        return viterbi_decode_soft(depuncture_soft(llr, n_info + _MEM), n_info)
    raise ValueError(f"unknown coding {coding!r}")


def coded_bit_count(n_info: int, coding: str) -> int:
    """Number of channel bits produced for n_info info bits under this code."""
    return int(coding_encode(np.zeros(n_info, dtype=np.int8), coding).size)


def info_bits_for_budget(n_channel_bits: int, coding: str) -> int:
    """Largest n_info whose coded bits fit in a channel budget (for sizing the payload)."""
    n = max(2, int(n_channel_bits * CODING_RATES[coding]) - _MEM)
    while n > 2 and coded_bit_count(n, coding) > n_channel_bits:
        n -= 1
    return n
