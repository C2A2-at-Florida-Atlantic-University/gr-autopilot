"""Hardware frequency hopping (Stage 3) — cycle a HopPlan across REAL channels and MEASURE the
jammed fraction, instead of folding it in analytically as ``InterferenceSimBackend`` does.

A real radio cannot time-average a jammer inside one capture; it must physically retune every dwell
and be caught (or miss) on each hop. This probe drives a live backend around the hop set, measures
the BER on each landing channel, and reports the *empirical* jammed fraction — the hardware analogue
of ``link/hopping.py``'s ``jammed_fraction``.

The jammer placement is left to the caller via ``before_hop`` (the demo retunes a physical HackRF
there): a **fixed** jammer never moves, so ``before_hop`` is a no-op; a **channel-following** jammer
is placed at ``history[t - dwells_behind]`` — where it was ``dwells_behind = round(tau / dwell)``
dwells ago. ``dwells_behind == 0`` is a follower fast enough to retune inside the dwell (jams the
current channel → the honest floor); ``>= 1`` is a follower slower than the link's hop (always a step
behind → evaded, as long as consecutive channels differ). The knee at ``tau == dwell`` is exactly the
sim's ``min_evading_hop_rate = 1/tau``, now measured on radios.

Backend-agnostic: a mock backend that models a jammer at its current channel exercises the same
aggregation in ``tests/test_hw_hopping.py`` with no radios.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gr_autopilot.control.objective import parse_modcod
from gr_autopilot.ledger import EditLedger, LedgerEntry
from gr_autopilot.link.backend import LinkBackend, LinkParams
from gr_autopilot.link.hopping import HopPlan
from gr_autopilot.scoring.metrics import compute_metrics


@dataclass
class HopRecord:
    hop_index: int
    channel_hz: float
    ber: float
    snr_db: float
    jammed: bool


@dataclass
class HopProbeResult:
    plan: HopPlan
    target_ber: float
    records: list = field(default_factory=list)

    @property
    def jammed_fraction(self) -> float:
        """Empirical fraction of hops the jammer actually hit (BER over target)."""
        return sum(r.jammed for r in self.records) / max(1, len(self.records))

    @property
    def aggregate_ber(self) -> float:
        """Time-averaged BER over the whole hop cycle (what the hopped link delivers)."""
        return float(np.mean([r.ber for r in self.records])) if self.records else 1.0

    @property
    def evaded(self) -> bool:
        """Hopped link usable: the time-averaged BER meets target (spread thin or fully evaded)."""
        return self.aggregate_ber <= self.target_ber


def follower_channel(history, current_hz: float, dwells_behind: int):
    """Where a channel-following jammer sits this hop: the link's channel ``dwells_behind`` dwells
    ago (0 = keeps up, jams the current channel). ``history`` is the channels visited so far
    (excluding the current one)."""
    if dwells_behind <= 0:
        return current_hz
    idx = len(history) - dwells_behind
    return history[idx] if idx >= 0 else None   # None before the follower has a target to chase


class HardwareHopProbe:
    """Cycle a hop set on a live backend and measure the jammed fraction empirically."""

    def __init__(self, backend: LinkBackend, hop_plan: HopPlan, target_ber: float = 1e-2,
                 probe_modcod: str = "qpsk", probe_bits: int = 6000, sps: int = 8,
                 rolloff: float = 0.35, settle_s: float | None = None,
                 ledger: EditLedger | None = None, seed: int = 0):
        self.backend = backend
        self.hop_plan = hop_plan
        self.channels = tuple(hop_plan.channels)
        self.target_ber = target_ber
        self.probe_modcod = probe_modcod
        self.probe_bits = probe_bits
        self.sps = sps
        self.rolloff = rolloff
        self.settle_s = settle_s          # per-hop dwell (backend.settle_s) if the backend has one
        self.ledger = ledger
        self.seed = seed

    def run(self, cycles: int = 1, before_hop=None) -> HopProbeResult:
        """Hop ``cycles`` times around the set. ``before_hop(hop_index, channel_hz, history)`` runs
        before each link trial — the demo uses it to place a physical jammer (fixed = no-op,
        follower = retune toward the link). ``history`` is the list of channels visited so far."""
        if self.settle_s is not None and hasattr(self.backend, "settle_s"):
            self.backend.settle_s = float(self.settle_s)
        mod, coding = parse_modcod(self.probe_modcod)
        recs: list[HopRecord] = []
        history: list[float] = []
        n = len(self.channels)
        for t in range(cycles * n):
            ch = self.channels[t % n]
            if before_hop is not None:
                before_hop(t, ch, list(history))
            history.append(ch)
            self.backend.set_condition(center_freq_hz=ch)
            r = self.backend.run_link(LinkParams(
                modulation=mod, coding=coding, n_payload_bits=self.probe_bits,
                sps=self.sps, rolloff=self.rolloff, seed=self.seed + t))
            m = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
            jammed = m.ber > self.target_ber
            recs.append(HopRecord(t, ch, m.ber, round(m.snr_db, 2), jammed))
            if self.ledger is not None:
                self.ledger.append(LedgerEntry(
                    iteration=t + 1, structure_id=f"hop{t}@{ch/1e6:.0f}MHz",
                    edit_description=f"hop to {ch/1e6:.0f} MHz ({self.hop_plan.hop_rate_hz:.1f} Hz)",
                    metrics={"BER": m.ber, "SNR_est": round(m.snr_db, 2)},
                    verdict="jammed" if jammed else "clear", loop="outer"))
        return HopProbeResult(self.hop_plan, self.target_ber, recs)
