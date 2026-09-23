"""Interference-aware simulation backend (Stage 2, hardware-free).

Mirrors the HackRF interference experiment behind the ``LinkBackend`` seam: a hidden co-channel
``Interferer`` degrades the link, and the agent's countermeasure is to retune the link center
frequency (``set_condition(center_freq_hz=...)``) away from it. The effective Es/(N0+I) comes
from the spectral-overlap physics in ``interference.py``; BER grading is delegated to the
theory-checked numpy AWGN backend at that effective SNR, so the frequency-avoidance controller
can be developed and unit-tested without radios and then run unchanged on the Plutos + HackRF.
"""
from __future__ import annotations

import logging
from dataclasses import replace

import numpy as np

log = logging.getLogger(__name__)

from gr_autopilot.link.backend import LinkBackend, LinkParams, LinkResult
from gr_autopilot.link.hopping import HopPlan, hopped_effective_es_n0_db, jammed_fraction
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.numpy_sim import NumpySimBackend


class InterferenceSimBackend(LinkBackend):
    name = "interference-sim"
    owns_channel = True  # the hidden channel (base SNR + interferer) lives here, not on the agent

    def __init__(self, clean_es_n0_db: float = 18.0, interferer: Interferer | None = None,
                 link_bw_hz: float = 350_000.0, center_freq_hz: float = 2.4e9,
                 hop_plan: HopPlan | None = None):
        self.clean_es_n0_db = float(clean_es_n0_db)
        self.interferer = interferer
        self.link_bw_hz = float(link_bw_hz)
        self.center_freq_hz = float(center_freq_hz)   # the link LO — the agent retunes this
        self.hop_plan = hop_plan                      # when set, the link hops (Stage 3)
        self.tx_power_db = 0.0                         # the agent's TX power knob (dB, relative) — it
                                                       # raises the SIGNAL vs both noise AND interference
        self._inner = NumpySimBackend()

    # Keys this backend understands. `es_n0_db` is accepted as an alias for `clean_es_n0_db` — the
    # numpy backend and the service default use that name, so passing it here must NOT silently grade
    # at the constructor default. Anything else is warned, never silently dropped.
    _KNOWN = frozenset({"center_freq_hz", "clean_es_n0_db", "es_n0_db", "tx_power_db",
                        "interferer", "hop_plan"})

    #: Everything the agent can drive here is applied analytically (see LinkBackend).
    applies_condition = frozenset({"center_freq_hz", "tx_power_db", "hop_plan"})

    def set_condition(self, **cond) -> None:
        # center_freq_hz / hop_plan / tx_power_db are the agent's actions; the interferer and base SNR
        # (the hidden path loss) are the framework-set channel.
        unknown = set(cond) - self._KNOWN
        if unknown:
            log.warning("InterferenceSimBackend.set_condition: ignoring unknown key(s) %s",
                        sorted(unknown))
        base = cond.get("clean_es_n0_db", cond.get("es_n0_db"))
        if base is not None:
            self.clean_es_n0_db = float(base)
        if cond.get("center_freq_hz") is not None:
            self.center_freq_hz = float(cond["center_freq_hz"])
        if cond.get("tx_power_db") is not None:
            self.tx_power_db = float(cond["tx_power_db"])
        if "interferer" in cond:
            self.interferer = cond["interferer"]
        if "hop_plan" in cond:
            self.hop_plan = cond["hop_plan"]   # set a HopPlan to hop, None to sit on one channel

    def effective_es_n0_db(self) -> float:
        # the agent's TX power raises Es relative to (N0 + I): a louder signal lifts the effective SINR
        # by tx_power_db, whether it is fighting noise (clean link) or a jammer.
        base = self.clean_es_n0_db + self.tx_power_db
        if self.interferer is None:
            return base
        if self.hop_plan is not None:  # Stage 3: hopping averages the interference over the dwell
            return hopped_effective_es_n0_db(self.interferer, self.hop_plan, self.link_bw_hz, base)
        return self.interferer.effective_es_n0_db(self.center_freq_hz, self.link_bw_hz, base)

    SENSE_MARGIN_DB = 3.0   # a channel reads occupied above this many dB over the noise floor

    def sense_spectrum(self, freqs_hz, bw_hz=None):
        """Monitor role (sim): received power over noise at each candidate frequency, link silent.
        A reactive interferer is invisible here -- it stays idle while the link is silent -- which
        is exactly the escalation the sensing countermeasure cannot beat."""
        bw = float(bw_hz) if bw_hz else self.link_bw_hz
        reactive = getattr(self.interferer, "reactive", False)
        out = []
        for f in freqs_hz:
            if self.interferer is None or reactive:
                power = 0.0
            else:
                power = self.interferer.sensed_power_db(float(f), bw)
            # carry the detection margin so the dashboard's threshold line matches THIS backend's
            # decision (the Pluto path uses a different margin) instead of a hardcoded value.
            out.append({"center_freq_hz": float(f), "power_db": round(power, 2),
                        "occupied": power > self.SENSE_MARGIN_DB,
                        "detect_margin_db": self.SENSE_MARGIN_DB})
        return out

    def run_link(self, params: LinkParams) -> LinkResult:
        if self.hop_plan is not None and self.interferer is not None:
            return self._run_hopped(params)
        eff = self.effective_es_n0_db()
        r = self._inner.run_link(replace(params, es_n0_db=eff))
        r.meta.update({
            "backend": self.name, "center_freq_hz": self.center_freq_hz,
            "effective_es_n0_db": eff, "clean_es_n0_db": self.clean_es_n0_db,
            "interferer_present": self.interferer is not None,
        })
        return r

    def _run_hopped(self, params: LinkParams) -> LinkResult:
        """Grade a hopped link PER-DWELL and concatenate the bits, matching the physical hardware
        path (``HoppingBackend``): a fraction of the transmission sees the jammer at full in-band
        interference and the rest sees the clean channel. BER is nonlinear in noise power, so grading
        every bit at a single time-averaged SNR (the earlier Jensen approximation) diverges from this
        per-dwell average — this keeps the sim figure consistent with the radios."""
        base = self.clean_es_n0_db + self.tx_power_db
        jam, hp = self.interferer, self.hop_plan
        if jam.reactive:
            # a follower jams a time fraction f of every dwell (on-channel), the rest is clean
            f = jammed_fraction(jam, hp, self.link_bw_hz)
            snr_jammed = jam.effective_es_n0_db(self.center_freq_hz, self.link_bw_hz, base)
            pops = [(f, snr_jammed), (1.0 - f, base)]
        else:
            # a fixed jammer hits the channels it overlaps; each channel is one equal dwell
            w = 1.0 / len(hp.channels)
            pops = [(w, jam.effective_es_n0_db(c, self.link_bw_hz, base)) for c in hp.channels]

        n = int(params.n_payload_bits)
        weights = [w for w, _ in pops]
        alloc = [int(round(w * n)) for w in weights]
        alloc[int(np.argmax(weights))] += n - sum(alloc)          # remainder on the largest dwell
        txb, rxb, txs, rxs, refs, modname = [], [], [], [], [], params.modulation
        for i, ((_, snr), nb) in enumerate(zip(pops, alloc)):
            if nb <= 0:
                continue
            r = self._inner.run_link(replace(params, es_n0_db=snr, n_payload_bits=nb,
                                             seed=params.seed + i))
            modname = r.modulation
            txb.append(r.tx_bits); rxb.append(r.rx_bits)
            txs.append(r.tx_syms); rxs.append(r.rx_syms); refs.append(r.ref_syms)
        return LinkResult(
            modulation=modname,
            tx_bits=np.concatenate(txb), rx_bits=np.concatenate(rxb),
            tx_syms=np.concatenate(txs), rx_syms=np.concatenate(rxs),
            ref_syms=np.concatenate(refs),
            meta={"backend": self.name, "center_freq_hz": self.center_freq_hz,
                  "effective_es_n0_db": self.effective_es_n0_db(),   # analytic scalar, reference only
                  "clean_es_n0_db": self.clean_es_n0_db, "interferer_present": True,
                  "hopped_per_dwell": True,
                  "jammed_fraction": jammed_fraction(jam, hp, self.link_bw_hz)},
        )
