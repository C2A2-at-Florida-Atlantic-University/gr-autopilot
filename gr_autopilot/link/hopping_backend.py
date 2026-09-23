"""HoppingBackend — make a hop plan PHYSICALLY hop on any real backend.

``InterferenceSimBackend`` folds a hop plan into an averaged effective SNR *analytically*. A real
radio cannot do that: it must actually retune every dwell and be caught (or miss) on each hop. This
decorator wraps any ``LinkBackend`` so the agent's ``set_hop_plan`` becomes real motion on hardware:

  * no hop plan set  -> a transparent pass-through to the inner backend (one channel, as before).
  * hop plan set     -> ``run_link`` cycles the hop set, retuning the inner backend to each channel,
    capturing a short sub-frame there, and concatenating the graded bits/symbols into ONE aggregate
    ``LinkResult`` — so the framework grades the hopped-link BER exactly as it grades a normal link.

An optional ``on_dwell(channel_hz)`` hook fires before each dwell so the *framework* can move a
follower jammer in lockstep (the turn-based, one-dwell-behind follower of the bench runs) — keeping jammer
control on the framework side of the integrity split, out of the agent's backend. Independently,
``live_channel_hz`` always names the channel the link is on RIGHT NOW, which is what a follower that
observes the air would see; the service prefers it over any schedule arithmetic.

Dwell honesty
-------------
``HopPlan.hop_rate_hz`` is what the agent ASKED for. What it gets is bounded below by how long one
graded capture takes (acquisition, DMA, settle), so the requested dwell and the achieved dwell are
different numbers and the experiment is only interpretable if both are reported. This backend sizes
each dwell's capture so its air time does not exceed the requested dwell (floored at
``min_dwell_bits``, below which nothing acquires), then TIMES every dwell and reports the result in
``meta['dwell_seconds']``. Nothing here silently stretches a dwell to fit the hardware.
"""
from __future__ import annotations

import time
from dataclasses import replace

import numpy as np

from gr_autopilot.link.backend import LinkBackend, LinkParams, LinkResult
from gr_autopilot.link.hopping import HopPlan


class HoppingBackend(LinkBackend):
    def __init__(self, inner: LinkBackend, on_dwell=None, min_dwell_bits: int = 2000):
        self.inner = inner
        self.on_dwell = on_dwell                 # framework hook: on_dwell(channel_hz) before a dwell
        self.min_dwell_bits = int(min_dwell_bits)
        self.hop_plan: HopPlan | None = None
        self.name = f"hopping/{inner.name}"
        self.owns_channel = inner.owns_channel
        # The channel the link is on right now (None until something tunes it). Updated per dwell.
        self.live_channel_hz: float | None = None
        # Our own record of the non-hopping tuning. The inner backend may not expose one at all --
        # WorkerBackend holds the radio in a child process and has neither `center_freq_hz` nor a
        # `condition` -- and without this the LO was never restored after a hop cycle.
        self._tuned_hz: float | None = None

    def __getattr__(self, name):
        # Transparent proxy: forward anything not defined on the decorator (settle_s, device handles,
        # retune hooks, ...) to the inner backend, instead of enumerating each attribute to forward.
        # __getattr__ only fires when normal lookup fails, so the decorator's own attrs/methods win.
        if name == "inner":
            raise AttributeError(name)   # not yet constructed / genuinely absent — avoid recursion
        return getattr(self.inner, name)

    @property
    def applies_condition(self) -> frozenset:
        """Whatever the inner backend applies, plus the hop plan THIS decorator applies.

        An inner backend that declares nothing means "unknown", not "applies nothing", so the
        decorator stays silent too -- declaring only ``{'hop_plan'}`` over an undeclared inner
        would make the service refuse the retune that inner in fact honours.
        """
        inner = frozenset(getattr(self.inner, "applies_condition", frozenset()))
        return (inner | {"hop_plan"}) if inner else frozenset()

    @property
    def center_freq_hz(self):
        if self._tuned_hz is not None:
            return self._tuned_hz
        # sim backends expose it directly; PlutoBackend keeps it in a `condition` dataclass
        if hasattr(self.inner, "center_freq_hz"):
            return self.inner.center_freq_hz
        cond = getattr(self.inner, "condition", None)
        return getattr(cond, "center_freq_hz", None)

    def set_condition(self, **cond) -> None:
        # Intercept the hop plan (this decorator owns it); pass everything else to the inner backend
        # (hidden channel, agent retune, gains).
        if "hop_plan" in cond:
            self.hop_plan = cond.pop("hop_plan")
        if cond.get("center_freq_hz") is not None:
            self._tuned_hz = float(cond["center_freq_hz"])
            if self.hop_plan is None:
                self.live_channel_hz = self._tuned_hz
        if cond:
            self.inner.set_condition(**cond)

    def sense_spectrum(self, freqs_hz, bw_hz=None):
        return self.inner.sense_spectrum(freqs_hz, bw_hz)

    # -- how many bits one dwell should grade -------------------------------
    def _dwell_bits(self, params: LinkParams, n_chans: int) -> int:
        """Per-dwell payload: the agent's request split across the hop set, but never longer in
        AIR TIME than the dwell it asked for, and never below the acquisition floor."""
        per = max(self.min_dwell_bits, params.n_payload_bits // n_chans)
        hp = self.hop_plan
        rate = getattr(self.inner, "sample_rate", None)
        if hp is None or not rate:
            return per
        try:
            from gr_autopilot import modulation
            bps = modulation.get(params.modulation).bits_per_symbol
        except Exception:                                    # unknown modulation — do not guess
            return per
        sym_rate = float(rate) / max(1, int(params.sps))
        budget = int(hp.dwell_s * sym_rate * bps)
        return max(self.min_dwell_bits, min(per, budget))

    def run_link(self, params: LinkParams) -> LinkResult:
        hp = self.hop_plan
        if hp is None or len(hp.channels) <= 1:
            return self.inner.run_link(params)                    # not hopping -> pass through

        chans = hp.channels
        per = self._dwell_bits(params, len(chans))
        orig_freq = self.center_freq_hz            # restore the inner LO after the hop cycle
        txb, rxb, txs, rxs, refs = [], [], [], [], []
        # Per-dwell books. BER over the cycle is what a hopped link delivers, but the cycle average
        # alone cannot say whether the interference was spread thin across every dwell or landed
        # whole on one of them -- which is the entire question a hopping experiment asks. Grading
        # stays in the scoring layer; these are the boundaries it needs to grade each dwell.
        dwell_bits, dwell_syms, dwell_secs = [], [], []
        modname = params.modulation
        for i, c in enumerate(chans):
            if self.on_dwell is not None:
                self.on_dwell(float(c))                           # framework moves its follower here
            t0 = time.perf_counter()
            self.live_channel_hz = float(c)
            self.inner.set_condition(center_freq_hz=float(c))
            r = self.inner.run_link(replace(params, n_payload_bits=per, seed=params.seed + i))
            dwell_secs.append(time.perf_counter() - t0)
            modname = r.modulation
            txb.append(np.asarray(r.tx_bits)); rxb.append(np.asarray(r.rx_bits))
            txs.append(np.asarray(r.tx_syms)); rxs.append(np.asarray(r.rx_syms))
            refs.append(np.asarray(r.ref_syms))
            # ACTUAL graded lengths, not the requested `per`: a capture cut short by a lost frame
            # grades fewer bits than it asked for, and slicing the aggregate on equal chunks would
            # attribute one dwell's errors to its neighbour.
            dwell_bits.append(int(np.asarray(r.rx_bits).size))
            dwell_syms.append(int(np.asarray(r.rx_syms).size))

        if orig_freq is not None:                  # leave the inner backend where it started, not on
            self.inner.set_condition(center_freq_hz=float(orig_freq))  # the last hop channel
            self.live_channel_hz = float(orig_freq)

        # Concatenate the per-dwell (already 1:1-aligned) captures into one aggregate result: the
        # BER over the whole hop cycle is what a hopped link delivers.
        return LinkResult(
            modulation=modname,
            tx_bits=np.concatenate(txb), rx_bits=np.concatenate(rxb),
            tx_syms=np.concatenate(txs), rx_syms=np.concatenate(rxs),
            ref_syms=np.concatenate(refs),
            meta={"backend": self.name, "hop_channels": [float(c) for c in chans],
                  "hop_rate_hz": hp.hop_rate_hz, "dwell_bits": per, "n_dwells": len(chans),
                  "dwell_bit_counts": dwell_bits, "dwell_sym_counts": dwell_syms,
                  "dwell_seconds": [round(s, 4) for s in dwell_secs],
                  "dwell_s_requested": round(hp.dwell_s, 6)})

    def close(self) -> None:
        if hasattr(self.inner, "close"):
            self.inner.close()
