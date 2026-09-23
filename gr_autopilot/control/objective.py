"""The constrained objective (spec §1, §10.1): meet a BER target, maximize spectral efficiency."""
from __future__ import annotations

from gr_autopilot.modulation import bits_per_symbol
from gr_autopilot.scoring.metrics import Metrics

# Code rates for the ladder's coding options (the "C" in AMC). A modcod is written "mod" or
# "mod:coding" (e.g. "16qam:conv_k3_r12"); coding cuts the throughput but adds coding gain.
CODE_RATES = {None: 1.0, "conv_k3_r12": 0.5, "conv_k3_r34": 0.75}


def parse_modcod(modcod: str) -> tuple[str, str | None]:
    """Split a ladder entry "mod[:coding]" into (modulation, coding-or-None)."""
    if ":" in modcod:
        mod, coding = modcod.split(":", 1)
        return mod, coding
    return modcod, None


def spectral_efficiency(modcod: str) -> float:
    """Effective bits per symbol: bits_per_symbol(mod) x code_rate — higher is better."""
    mod, coding = parse_modcod(modcod)
    return float(bits_per_symbol(mod)) * CODE_RATES.get(coding, 1.0)


def occupied_bw_factor(rolloff: float) -> float:
    """RRC occupied bandwidth relative to the symbol rate: (1 + rolloff)."""
    return 1.0 + float(rolloff)


def bits_per_hz(modulation: str, rolloff: float) -> float:
    """Spectral efficiency accounting for pulse shaping: bits/symbol / (1 + rolloff)."""
    return spectral_efficiency(modulation) / occupied_bw_factor(rolloff)


def bo_loss(metrics: Metrics) -> float:
    """Scalar the BO inner loop MINIMIZES for a fixed structure.

    Primary term is BER; a small EVM term provides a gradient below the BER measurement
    floor so tuning (e.g. phase correction) keeps improving even once BER hits zero.
    """
    return float(metrics.ber) + 1e-4 * float(metrics.evm_pct) / 100.0


def bandwidth_loss(metrics: Metrics, rolloff: float, target_ber: float) -> float:
    """Inner-loop loss for the spectral-efficiency objective: minimize occupied bandwidth
    (1 + rolloff) subject to BER <= target. Any BER-failing config is ranked strictly worse
    than every feasible one, so the BO drives toward the tightest pulse that still decodes.
    """
    if float(metrics.ber) <= target_ber:
        return occupied_bw_factor(rolloff)          # feasible: <= ~1.6, minimize bandwidth
    return 10.0 + float(metrics.ber)                # infeasible: always worse than any feasible


def choose_modcod(per_mod: dict, target_ber: float, ladder) -> str:
    """Pick the highest-spectral-efficiency modulation whose BER meets the target.

    If none meet it (outage), fall back to the most robust (first) rung of the ladder.
    """
    meeting = [m for m in ladder if m in per_mod and per_mod[m].ber <= target_ber]
    if meeting:
        return max(meeting, key=spectral_efficiency)
    return ladder[0]
