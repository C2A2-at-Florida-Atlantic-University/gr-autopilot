"""CFO-robust data-aided frame synchronization (Zadoff-Chu preamble).

Framework-owned synchronization primitives. The receiver in ``pluto.py`` originally acquired
*blind-first*: non-data-aided Gardner timing recovery and a Costas carrier loop had to lock
before the preamble could even be correlated. Both loops need a healthy SNR to pull in, so the
whole receiver stopped acquiring around ~11 dB — above the ~7-10 dB region where the BPSK vs
QPSK crossover (the 3rd AMC rung) lives, so that rung was unreachable on hardware.

This module flips the ordering to **data-aided acquisition first**. A Zadoff-Chu (ZC) preamble
is a constant-modulus, near-perfect-autocorrelation sequence (the same family LTE uses for its
PSS). Correlating against it gives a timing estimate with a processing gain of ~10*log10(N) dB
*before* any tracking loop, so acquisition survives well below the blind-loop floor.

The one hazard is the ~+-120 kHz carrier-frequency offset (CFO) between two free-running Pluto
oscillators: a single long *coherent* ZC correlation is destroyed by it (the phase winds across
the window). Two mechanisms handle that, neither of which needs a tracking loop:

1. **Schmidl-Cox timing** -- the preamble is two identical ZC halves ``[z | z]``. The delay-D
   autocorrelation ``P[d] = sum conj(r[d+k]) r[d+k+D]`` peaks at the preamble start, and its
   *magnitude* is CFO-insensitive (the CFO is common to both halves and cancels in the
   conjugate product). This detects the frame and gives coarse timing at any CFO.
2. **FFT-tone CFO** -- de-spreading the received preamble by the known ZC (``r * conj(z)``) turns
   it into a pure tone at the CFO (ZC is constant-modulus, so ``|z|^2 = 1`` removes the
   modulation exactly). One FFT locates that tone over the full +-symrate/2 range at fine
   resolution -- covering the +-120 kHz worst case -- with the preamble length as processing gain.

After coarse timing + CFO removal, residual carrier phase is small and a light decision-directed
PLL cleans it up. The whole chain is pure numpy (no GNU Radio) so it is unit-testable against a
synthetic pulse-shaped channel with fractional delay, CFO, and AWGN -- see ``tests/test_sync.py``.
"""
from __future__ import annotations

import numpy as np

# Default ZC parameters. Root must be coprime to the half length; 25 is coprime to 64.
_DEFAULT_ROOT = 25
_DEFAULT_HALF = 64


def zadoff_chu(length: int, root: int = _DEFAULT_ROOT) -> np.ndarray:
    """Length-``length`` Zadoff-Chu sequence with the given root (constant modulus, |z|=1).

    Uses the standard even/odd forms. ``gcd(root, length)`` should be 1 for the ideal
    autocorrelation; this is the caller's responsibility.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    if np.gcd(int(root), int(length)) != 1:
        raise ValueError(f"root {root} must be coprime to length {length}")
    n = np.arange(length)
    if length % 2 == 0:
        phase = np.pi * root * n * n / length
    else:
        phase = np.pi * root * n * (n + 1) / length
    return np.exp(-1j * phase).astype(np.complex128)


def zc_preamble(half_len: int = _DEFAULT_HALF, root: int = _DEFAULT_ROOT) -> np.ndarray:
    """Framework-owned frame-sync preamble: two identical ZC halves ``[z | z]``.

    The repetition is what makes Schmidl-Cox timing CFO-insensitive. |z| = 1, so the preamble
    carries unit average symbol energy -- matching the unit-energy payload constellations, so the
    transmitter's single amplitude scale applies uniformly across preamble and payload.
    """
    z = zadoff_chu(half_len, root)
    return np.concatenate([z, z])


def rrc_taps(sps: int, rolloff: float, span_syms: int = 8) -> np.ndarray:
    """Root-raised-cosine taps, unit-energy, ``span_syms*sps + 1`` long (numpy, for tests).

    The hardware receiver uses GNU Radio's ``firdes.root_raised_cosine``; this is an
    independent numpy implementation so the synchronization tests need no GNU Radio.
    """
    N = span_syms * sps
    t = (np.arange(N + 1) - N / 2) / sps  # time in symbols, symmetric about 0
    beta = float(rolloff)
    h = np.empty_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-8:
            h[i] = 1.0 - beta + 4.0 * beta / np.pi
        elif beta > 0 and abs(abs(ti) - 1.0 / (4.0 * beta)) < 1e-8:
            h[i] = (beta / np.sqrt(2.0)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * beta))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta))
            )
        else:
            num = np.sin(np.pi * ti * (1 - beta)) + 4 * beta * ti * np.cos(np.pi * ti * (1 + beta))
            den = np.pi * ti * (1 - (4 * beta * ti) ** 2)
            h[i] = num / den
    h /= np.sqrt(np.sum(h ** 2))
    return h


def schmidl_cox_metric(r: np.ndarray, D: int) -> tuple[np.ndarray, np.ndarray]:
    """Delay-``D`` autocorrelation timing metric for a ``[z | z]`` preamble.

    Returns ``(M, P)`` where ``P[d] = sum_{k<D} conj(r[d+k]) r[d+k+D]`` and
    ``M[d] = |P[d]|^2 / R[d]^2`` with ``R[d] = sum_{k<D} |r[d+k+D]|^2``. ``M`` peaks (~1) at the
    preamble start and is CFO-insensitive in magnitude. ``P``'s phase there is the CFO over D
    samples (used only when its unambiguous range suffices; the FFT-tone estimator is primary).
    """
    r = np.asarray(r, dtype=np.complex128)
    N = r.size
    if D <= 0 or N < 2 * D + 1:
        return np.zeros(0), np.zeros(0)
    prod = np.conj(r[: N - D]) * r[D:]                 # prod[n] = conj(r[n]) r[n+D]
    cs = np.concatenate([[0.0 + 0j], np.cumsum(prod)])
    P = (cs[D:] - cs[:-D])[: N - 2 * D + 1]            # windowed sum, length N-2D+1
    energy = np.abs(r[D:]) ** 2
    ce = np.concatenate([[0.0], np.cumsum(energy)])
    R = (ce[D:] - ce[:-D])[: N - 2 * D + 1]
    M = np.abs(P) ** 2 / (R ** 2 + 1e-12)
    return M, P


def acquire(
    mf: np.ndarray,
    sps: int,
    half_len: int = _DEFAULT_HALF,
    root: int = _DEFAULT_ROOT,
    search: int | None = None,
    detect_thresh: float = 0.30,
    tone_thresh: float = 6.0,
    cfo_max: float = 0.15,
    nfft: int = 4096,
) -> dict | None:
    """Detect the ZC preamble in a matched-filtered stream and estimate timing + CFO + phase.

    ``mf`` is the matched-filtered baseband at ``sps`` samples/symbol. Returns a dict with
    ``offset`` (sample index of the preamble start), ``cfo`` (cycles per symbol), ``theta``
    (coarse carrier phase), and confidence fields (``metric`` = S&C peak, ``tone`` = FFT-tone
    sharpness), or ``None`` if no candidate yields a real ZC tone.

    Pipeline: Schmidl-Cox proposes the top-K CFO-robust timing candidates; for each, a small
    joint search over sample offset (+-``search``) maximizes the de-spread FFT-tone, which yields
    both a refined integer-sample timing and the CFO. The candidate with the strongest tone wins
    and must clear ``tone_thresh`` -- only a genuine ZC preamble de-spreads to a sharp tone.

    **The Zadoff-Chu shift/frequency duality** is the subtlety: a whole-symbol timing error of
    ``m`` maps the de-spread tone to ``true_cfo + root*m/L`` (mod 1), so a mis-aligned offset can
    also produce a sharp -- but *aliased* -- tone. Three guards defeat it: (1) ``search`` spans
    +-4 symbols -- wide enough to cover the S&C metric's timing spread, yet the shift-aliases for
    ``|m| = 1..4`` (``root*m/L`` = 0.39/0.22/0.17/0.44 cyc/sym for the defaults) all sit above
    (2) the plausible-CFO band ``|cfo| <= cfo_max`` the tone search is constrained to, and
    ``|m| >= 5`` is outside the window; (3) among offsets that clear the tone gate, selection is
    by data-aided preamble EVM, which the true alignment minimizes. ``cfo_max`` must exceed the
    link's worst-case CFO yet stay below ``root/L``; the default 0.15 covers the two-Pluto offset
    with margin at the bench symbol rate.
    """
    mf = np.asarray(mf, dtype=np.complex128)
    if search is None:
        search = 4 * sps  # +-4 symbols: covers the S&C timing spread; |m|=1..4 aliases are out-of-band
    D = half_len * sps
    M, _ = schmidl_cox_metric(mf, D)
    if M.size == 0:
        return None
    z2 = zc_preamble(half_len, root)
    npre = z2.size
    freqs = np.fft.fftfreq(nfft)
    inband = np.abs(freqs) <= cfo_max  # reject the root*m/L shift-aliases

    mag = M.copy()  # top-K well-separated S&C candidates above the detection threshold
    cands = []
    for _ in range(8):
        d = int(np.argmax(mag))
        if mag[d] < detect_thresh:
            break
        cands.append((d, float(M[d])))
        mag[max(0, d - D): d + D] = 0.0
    if not cands:
        return None

    # For each (candidate, sub-symbol offset): estimate CFO from the in-band de-spread tone, then
    # score by how well the de-rotated preamble reconstructs the KNOWN ZC (data-aided EVM). The
    # tone gate is detection; the EVM is disambiguation -- the true alignment reconstructs z2
    # almost perfectly, while a fractional/aliased offset that sneaks an in-band tone does not.
    kk = np.arange(npre)
    best = None  # (evm, offset, cfo, theta, tone, metric)
    for d0, metric in cands:
        for off in range(max(0, d0 - search), d0 + search + 1):
            seg = mf[off: off + npre * sps: sps]
            if seg.size < npre:
                continue
            seg = seg[:npre]
            F = np.abs(np.fft.fft(seg * np.conj(z2), nfft))  # de-spread -> tone at CFO
            k = int(np.argmax(np.where(inband, F, 0.0)))     # constrain to plausible CFO band
            tone = float(F[k] / (F.mean() + 1e-12))
            if tone < tone_thresh:
                continue
            cfo = float(freqs[k])
            despun = seg * np.exp(-1j * 2 * np.pi * cfo * kk)
            theta = float(np.angle(np.vdot(z2, despun)))
            rec = despun * np.exp(-1j * theta)
            rec = rec / (np.sqrt(np.mean(np.abs(rec) ** 2)) + 1e-12)
            evm = float(np.mean(np.abs(rec - z2) ** 2))       # vs known ZC (both unit modulus)
            if best is None or evm < best[0]:
                best = (evm, off, cfo, theta, tone, metric)
    if best is None:
        return None
    evm, off, cfo, theta, tone, metric = best
    return {
        "offset": int(off), "cfo": cfo, "theta": theta,
        "metric": metric, "tone": tone, "preamble_evm": evm,
        "half_len": half_len, "root": root, "npre": npre, "sps": int(sps),
    }


def extract_symbols(mf: np.ndarray, acq: dict, n_total: int) -> np.ndarray:
    """Decimate + coarse-correct ``n_total`` symbols starting at the preamble.

    Index 0 is the first preamble symbol; the CFO/phase de-rotation is continuous across the
    whole frame, so ``result[npre:]`` are the payload symbols (coarse-corrected), ready for a
    residual carrier tracker.

    ASSUMES NEGLIGIBLE SAMPLING-CLOCK OFFSET over the frame: it decimates on ONE integer-sample grid
    chosen at the preamble, and the downstream ``dd_carrier`` tracks carrier phase but NOT symbol
    timing (unlike the legacy Gardner front-end it replaces). At the bench's ~25 ppm oscillator
    tolerance and sps=8, timing drifts ~0.5 symbol over a ~40k-bit QPSK frame, so the frame tail
    decodes at elevated BER while the head is clean. Keep ZC-path frames short (or add a pilot-anchored
    per-block timing re-estimate) for long captures; the paper discloses this assumption."""
    off, cfo, theta, sps = acq["offset"], acq["cfo"], acq["theta"], acq["sps"]
    grid = mf[off:: sps][:n_total]
    k = np.arange(grid.size)
    return grid * np.exp(-1j * (2 * np.pi * cfo * k + theta))


# Pilot-symbol-aided modulation (PSAM): the rotation-invariant carrier recovery for 16-QAM.
# The decision-directed loop below *decides* the phase and can walk to a 90°-rotated alias of the
# (rotationally symmetric) 16-QAM constellation at marginal SNR — benign for an uncoded link, fatal
# for a coded one (a constant rotation permutes every symbol's bits -> Viterbi garbage, as
# measured on the bench). PSAM instead *measures* the absolute phase at KNOWN pilot
# symbols sprinkled through the payload and interpolates between them: no loop, so no slip, and the
# pilots pin the absolute phase (the 90° offset cannot survive). Overhead is 1 pilot per `spacing`.
PILOT_SYM = 1.0 + 0.0j  # a known unit-modulus reference (phase 0); amplitude matches 16-QAM's mid ring


def psam_layout(n_data: int, spacing: int) -> tuple[int, np.ndarray]:
    """Deterministic PSAM frame layout: a pilot before each block of ``spacing`` data symbols.
    Returns ``(total_len, is_pilot)`` — both ends compute the same mask, so no side-channel."""
    if spacing < 1:
        raise ValueError("pilot spacing must be >= 1")
    n_blocks = (n_data + spacing - 1) // spacing
    total = n_data + n_blocks
    is_pilot = np.zeros(total, dtype=bool)
    pos, remaining = 0, n_data
    for _ in range(n_blocks):
        is_pilot[pos] = True
        take = min(spacing, remaining)
        pos += 1 + take
        remaining -= take
    return total, is_pilot


def psam_insert(data_syms: np.ndarray, spacing: int, pilot: complex = PILOT_SYM) -> tuple[np.ndarray, np.ndarray]:
    """Interleave ``pilot`` before each block of ``spacing`` data symbols. Returns ``(frame, is_pilot)``."""
    data = np.asarray(data_syms, dtype=np.complex128)
    total, is_pilot = psam_layout(data.size, spacing)
    frame = np.empty(total, dtype=np.complex128)
    frame[is_pilot] = pilot
    frame[~is_pilot] = data
    return frame, is_pilot


def psam_derotate(rx_frame: np.ndarray, is_pilot: np.ndarray, pilot: complex = PILOT_SYM) -> np.ndarray:
    """Recover the data symbols from a pilot-interleaved frame by measuring the absolute phase at each
    pilot, unwrapping, and interpolating across the payload — rotation-invariant, slip-proof.

    ``rx_frame`` is the received payload-with-pilots (coarse-derotated already). Returns just the data
    symbols (pilots removed), phase-corrected against the known pilot reference.
    """
    rx = np.asarray(rx_frame, dtype=np.complex128)
    is_pilot = np.asarray(is_pilot, dtype=bool)[: rx.size]
    idx = np.arange(rx.size)
    pil_idx = idx[is_pilot]
    if pil_idx.size < 1:
        return rx[~is_pilot]
    # absolute phase at each pilot (pilot is a known reference), unwrapped so a 90° slip between two
    # pilots is followed continuously rather than aliased away
    ep = np.unwrap(np.angle(rx[pil_idx] * np.conj(pilot)))
    phase = np.interp(idx, pil_idx, ep)          # piecewise-linear across the payload; holds the ends
    return (rx * np.exp(-1j * phase))[~is_pilot]


def psam_slip_correct(dd_syms: np.ndarray, is_pilot: np.ndarray, pilot: complex) -> np.ndarray:
    """Remove the residual DISCRETE 90° rotation from a decision-directed-tracked stream using known
    pilots — the hybrid that actually works on hardware.

    ``psam_derotate`` (continuous interpolation of pilot phases) is noise-limited: a single noisy
    pilot per block, plus the odd unwrap error, injects more phase noise than the DD loop removes.
    But the DD loop's *only* real failure is settling on a **constant** 90°-rotated alias of the
    payload (measured on the bench). So let DD do the low-noise fine tracking, and use the
    pilots to resolve just the single global quantized ambiguity: round ``angle(rx_pilot / pilot)`` to
    the nearest 90° at each pilot, then take the **majority vote** across all pilots. The vote is what
    makes it robust — the DD loop needs ~50-100 symbols to pull in the residual CFO, so the first few
    pilots sit in that transient and can read a spurious rotation; per-pilot correction would turn
    those into error bursts, but they are outvoted by the steady-state majority.

    ``pilot`` should be a constellation point (so the DD loop decides it correctly and is not
    perturbed) — a corner point is ideal (strongest, four distinct 90° images). Returns the data
    symbols (pilots removed).
    """
    x = np.asarray(dd_syms, dtype=np.complex128)
    is_pilot = np.asarray(is_pilot, dtype=bool)[: x.size]
    pil = x[is_pilot]
    if pil.size == 0:
        return x[~is_pilot]
    k_per = np.mod(np.round(np.angle(pil * np.conj(pilot)) / (np.pi / 2.0)), 4).astype(int)
    k = int(np.bincount(k_per, minlength=4).argmax())          # global rotation by majority vote
    return (x * np.exp(-1j * k * (np.pi / 2.0)))[~is_pilot]


def dd_carrier(x: np.ndarray, points: np.ndarray, mu1: float = 0.05, mu2: float = 0.001) -> np.ndarray:
    """2nd-order decision-directed carrier PLL (residual phase/frequency after ZC acquisition).

    Constellation-agnostic (uses nearest-point decisions), so it serves BPSK/QPSK and 16-QAM
    alike. Assumes the input is already coarse-derotated (CFO + preamble phase removed), so the
    initial decisions are reliable and the loop only cleans small residual drift.
    """
    x = np.asarray(x, dtype=np.complex128)
    points = np.asarray(points, dtype=np.complex128)
    phi = 0.0
    freq = 0.0
    out = np.empty_like(x)
    for i in range(x.size):
        y = x[i] * np.exp(-1j * phi)
        out[i] = y
        d = points[np.argmin(np.abs(y - points))]
        e = float(np.angle(y * np.conj(d)))
        freq += mu2 * e
        phi += mu1 * e + freq
    return out
