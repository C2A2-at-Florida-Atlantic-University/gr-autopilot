"""Frequency-hopping controller (Stage 3) — evade a reactive jammer by out-hopping its reaction.

When sensed/blind avoidance fails on a REACTIVE jammer (Stage 2: it follows the link, so a
sensed-clean channel still gets jammed — ``beaten=False``), the countermeasure is to HOP: change
frequency faster than the jammer can detect-and-retune. This controller **escalates the hop rate**
until the measured BER meets the target, discovering the minimum evading rate *from measurement*
without ever being told the jammer's reaction time — then runs AMC on the evaded hopped link. If no
achievable rate evades, it reports an honest failure (the reason hopping has a floor: a fast enough
reactive jammer out-reacts any hop rate).

Backend-agnostic: same controller for ``InterferenceSimBackend`` (hop_plan-aware) and, later, a
fast-retuning radio.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gr_autopilot.control.controller import ControllerResult, TwoLoopController
from gr_autopilot.control.objective import parse_modcod
from gr_autopilot.ledger import EditLedger, LedgerEntry
from gr_autopilot.link.backend import LinkBackend, LinkParams
from gr_autopilot.link.hopping import HopPlan
from gr_autopilot.scoring.metrics import compute_metrics


@dataclass
class HoppingResult:
    evaded: bool
    hop_rate_hz: float | None                        # min hop rate that met target (None if never)
    amc: ControllerResult | None = None              # AMC outcome on the evaded hopped link
    beaten: bool = False                             # link actually restored (target met)?
    trajectory: list = field(default_factory=list)   # [{hop_rate_hz, ber, snr_db, evaded}]


class FrequencyHoppingController:
    def __init__(self, backend: LinkBackend, channels, hop_rates, target_ber: float = 1e-2,
                 probe_modcod: str = "qpsk", probe_bits: int = 8000,
                 ladder=("bpsk", "qpsk", "16qam"), confirm_bits: int = 8000,
                 sps: int = 8, rolloff: float = 0.35, ledger: EditLedger | None = None, seed: int = 0):
        self.backend = backend
        self.channels = tuple(float(f) for f in channels)     # the hop set
        self.hop_rates = sorted(float(r) for r in hop_rates)  # escalate low -> high
        self.target_ber = target_ber
        self.probe_modcod = probe_modcod
        self.probe_bits = probe_bits
        self.ladder = tuple(ladder)
        self.confirm_bits = confirm_bits
        self.sps = sps
        self.rolloff = rolloff
        self.ledger = ledger
        self.seed = seed

    def _probe(self, hop_plan: HopPlan):
        self.backend.set_condition(hop_plan=hop_plan)
        mod, coding = parse_modcod(self.probe_modcod)
        r = self.backend.run_link(LinkParams(
            modulation=mod, coding=coding, n_payload_bits=self.probe_bits,
            sps=self.sps, rolloff=self.rolloff, seed=self.seed))
        return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)

    def run(self) -> HoppingResult:
        traj = []
        for i, rate in enumerate(self.hop_rates):
            hp = HopPlan(self.channels, rate)
            m = self._probe(hp)
            evaded = m.ber <= self.target_ber
            traj.append({"hop_rate_hz": rate, "ber": m.ber, "snr_db": round(m.snr_db, 2),
                         "evaded": evaded})
            if self.ledger is not None:
                self.ledger.append(LedgerEntry(
                    iteration=i + 1, structure_id=f"hop@{rate:.0f}Hz",
                    edit_description=f"hop {len(self.channels)} channels at {rate:.0f} Hz",
                    metrics={"BER": m.ber, "SNR_est": round(m.snr_db, 2)},
                    verdict="evaded" if evaded else "jammed", loop="outer"))
            if evaded:
                # out-hopped the jammer -> AMC on the evaded hopped link for the best modcod
                self.backend.set_condition(hop_plan=hp)
                amc = TwoLoopController(
                    self.backend, target_ber=self.target_ber, ladder=self.ladder, knobs={},
                    confirm_bits=self.confirm_bits, sps=self.sps, rolloff=self.rolloff,
                    ledger=self.ledger, seed=self.seed).run({})
                beaten = amc.per_mod[amc.chosen].ber <= self.target_ber
                return HoppingResult(evaded=True, hop_rate_hz=rate, amc=amc, beaten=beaten,
                                     trajectory=traj)
        return HoppingResult(evaded=False, hop_rate_hz=None, beaten=False, trajectory=traj)
