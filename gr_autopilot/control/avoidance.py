"""Frequency-avoidance controller (Stage 2): adapt to a jammer by retuning the link.

The adversarial objective -- meet a BER target, maximize spectral efficiency, while an interferer
occupies part of the band -- adds one structural action on top of AMC: **move the link's center
frequency**. Two ways to choose where:

* **blind** -- probe candidate frequencies with a full link trial each, keep the first that meets
  the target (works with no monitor, but spends a trial per candidate);
* **sensed** (monitor role, spec §6) -- with the link silent, energy-detect received power across
  the candidates, then retune straight to a clean one. One spectrum sweep + one targeted move,
  and it yields an occupancy map. This is the default when the backend has a monitor.

The controller is never told where the interferer is. ``beaten`` reports whether the link was
actually restored on the chosen channel -- a *sensed-clean channel that still fails* is the
signature of a **reactive** jammer (idle while the link is silent, so energy detection misses it,
then following the link when it transmits): sensing can't beat that, which motivates hopping.

Backend-agnostic: the same controller runs against ``InterferenceSimBackend`` and the
Plutos-plus-HackRF bench unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gr_autopilot.control.controller import ControllerResult, TwoLoopController
from gr_autopilot.control.objective import parse_modcod
from gr_autopilot.ledger import EditLedger, LedgerEntry
from gr_autopilot.link.backend import LinkBackend, LinkParams
from gr_autopilot.scoring.metrics import compute_metrics


@dataclass
class AvoidanceResult:
    jammed_freq_hz: float
    chosen_freq_hz: float | None                 # None if no channel was chosen
    mode: str                                    # "sensed" | "blind"
    amc: ControllerResult | None = None          # AMC outcome on the chosen channel
    beaten: bool = False                         # link actually restored (target met) on it?
    scan: list = field(default_factory=list)     # blind probe: {freq, ber, snr, clean}
    occupancy: list = field(default_factory=list)  # sensed spectrum: {freq, power_db, occupied}


class FrequencyAvoidanceController:
    def __init__(self, backend: LinkBackend, candidates, ladder=("bpsk", "qpsk", "16qam"),
                 target_ber: float = 1e-2, probe_modcod: str = "bpsk", probe_bits: int = 8000,
                 confirm_bits: int = 8000, sps: int = 8, rolloff: float = 0.35,
                 ledger: EditLedger | None = None, seed: int = 0, use_monitor: bool = True,
                 sense_bw_hz: float | None = None):
        self.backend = backend
        self.candidates = [float(f) for f in candidates]  # candidates[0] = current (jammed) freq
        self.ladder = tuple(ladder)
        self.target_ber = target_ber
        self.probe_modcod = probe_modcod
        self.probe_bits = probe_bits
        self.confirm_bits = confirm_bits
        self.sps = sps
        self.rolloff = rolloff
        self.ledger = ledger
        self.seed = seed
        self.use_monitor = use_monitor
        self.sense_bw_hz = sense_bw_hz

    # -- shared: run AMC on a chosen channel, report whether it actually met the target --
    def _amc(self, freq: float):
        self.backend.set_condition(center_freq_hz=freq)
        # full two-loop on the chosen channel (default inner loop = phase-correction BO); this is
        # what restores the highest rung after a retune. (Passing knobs={} would disable the inner
        # loop; historically an empty dict was falsy and silently ran BO anyway -- see controller.)
        amc = TwoLoopController(
            self.backend, target_ber=self.target_ber, ladder=self.ladder,
            confirm_bits=self.confirm_bits, sps=self.sps, rolloff=self.rolloff,
            ledger=self.ledger, seed=self.seed).run({})
        beaten = amc.per_mod[amc.chosen].ber <= self.target_ber
        return amc, beaten

    def _probe(self, freq: float):
        self.backend.set_condition(center_freq_hz=freq)
        mod, coding = parse_modcod(self.probe_modcod)
        r = self.backend.run_link(LinkParams(
            modulation=mod, coding=coding, n_payload_bits=self.probe_bits,
            sps=self.sps, rolloff=self.rolloff, seed=self.seed))
        return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)

    def run(self) -> AvoidanceResult:
        occ = self.backend.sense_spectrum(self.candidates, self.sense_bw_hz) if self.use_monitor else None
        return self._run_sensed(occ) if occ is not None else self._run_blind()

    # -- monitor role: sense the band, retune straight to a clean channel --
    def _run_sensed(self, occ: list) -> AvoidanceResult:
        for i, o in enumerate(occ):
            if self.ledger is not None:
                self.ledger.append(LedgerEntry(
                    iteration=i + 1, structure_id=f"sense@{o['center_freq_hz']/1e6:.3f}MHz",
                    edit_description=f"monitor: sense {o['center_freq_hz']/1e6:.3f} MHz",
                    metrics={"power_db": o["power_db"]},
                    verdict="occupied" if o["occupied"] else "clear", loop="outer"))
        clear = [o for o in occ if not o["occupied"]]
        chosen = min(clear, key=lambda o: o["power_db"])["center_freq_hz"] if clear else None
        amc, beaten = (None, False)
        if chosen is not None:
            amc, beaten = self._amc(chosen)
        return AvoidanceResult(jammed_freq_hz=self.candidates[0], chosen_freq_hz=chosen,
                               mode="sensed", amc=amc, beaten=beaten, occupancy=occ)

    # -- blind: probe each candidate with a full link trial --
    def _run_blind(self) -> AvoidanceResult:
        scan, chosen = [], None
        for i, f in enumerate(self.candidates):
            m = self._probe(f)
            clean = m.ber <= self.target_ber
            scan.append({"center_freq_hz": f, "ber": m.ber, "snr_db": m.snr_db, "clean": clean})
            if self.ledger is not None:
                self.ledger.append(LedgerEntry(
                    iteration=i + 1, structure_id=f"probe_{self.probe_modcod}@{f/1e6:.3f}MHz",
                    edit_description=("probe current channel" if i == 0 else
                                      f"scan: retune to {f/1e6:.3f} MHz"),
                    metrics={"BER": m.ber, "SNR_est": round(m.snr_db, 2)},
                    verdict="jammed" if not clean else "clean", loop="outer"))
            if clean and chosen is None:
                chosen = f
                break
        amc, beaten = (None, False)
        if chosen is not None:
            amc, beaten = self._amc(chosen)
        return AvoidanceResult(jammed_freq_hz=self.candidates[0], chosen_freq_hz=chosen,
                               mode="blind", amc=amc, beaten=beaten, scan=scan)
