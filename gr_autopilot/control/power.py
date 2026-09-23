"""Power adaptation — the fifth countermeasure (alongside AMC, coding, avoidance, hopping).

The agent owns a TX-power knob but not the path loss, so it finds the power it needs by MEASUREMENT.
Three uses, all here:

  * efficiency  — the minimum power that still meets the BER target at a modcod ("transmit no louder
    than necessary": least energy, least interference footprint);
  * power-vs-rate — spend power to HOLD a higher modulation instead of dropping the rate;
  * power-vs-jammer — boost SINR to overcome an interferer, up to a finite power budget (a real PA
    ceiling) — beyond which power alone cannot, and the agent must avoid/hop.

Effective SINR is monotonic in TX power, so the minimum meeting power is found by bisection on the
grader. ``adapt`` returns the highest-efficiency modcod feasible within the budget, at its minimum
power — max rate, least power — or ``None`` when even the lowest rung is infeasible (hand off to
avoidance/hopping).
"""
from __future__ import annotations

from dataclasses import dataclass

from gr_autopilot.control.objective import parse_modcod, spectral_efficiency
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring.metrics import compute_metrics


@dataclass
class PowerResult:
    modcod: str
    min_power_db: float | None       # least TX power meeting target (None if infeasible within budget)
    feasible: bool
    ber: float
    efficiency: float = 0.0


class PowerController:
    def __init__(self, backend, target_ber: float = 1e-2, bits: int = 40_000, sps: int = 8,
                 rolloff: float = 0.35, seed: int = 0, budget_db=(-20.0, 6.0),
                 channel_kwargs: dict | None = None):
        if not getattr(backend, "owns_channel", False):
            # tx_power_db goes through set_condition, a no-op on a backend that doesn't own its
            # channel — every power level would then read identical BER and min_power would fabricate
            # a result. Fail loudly instead.
            raise ValueError(f"PowerController needs an owns_channel backend; "
                             f"{getattr(backend, 'name', backend)!r} does not control tx_power_db")
        self.backend = backend
        self.target_ber = target_ber
        self.bits = bits
        self.sps = sps
        self.rolloff = rolloff
        self.seed = seed
        self.lo, self.hi = float(budget_db[0]), float(budget_db[1])
        self.channel_kwargs = dict(channel_kwargs or {})

    def _probe(self, modcod: str, power_db: float) -> tuple[bool, float]:
        mod, coding = parse_modcod(modcod)
        self.backend.set_condition(tx_power_db=float(power_db))
        r = self.backend.run_link(LinkParams(modulation=mod, coding=coding, n_payload_bits=self.bits,
                                             sps=self.sps, rolloff=self.rolloff, seed=self.seed,
                                             **self.channel_kwargs))
        ber = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms).ber
        return ber <= self.target_ber, ber

    def min_power(self, modcod: str, iters: int = 8) -> PowerResult:
        """Least TX power (dB) meeting the target for ``modcod``, by bisection over the budget."""
        eff = spectral_efficiency(modcod)
        meets_hi, ber_hi = self._probe(modcod, self.hi)
        if not meets_hi:
            return PowerResult(modcod, None, False, ber_hi, eff)     # even max power fails
        meets_lo, ber_lo = self._probe(modcod, self.lo)
        if meets_lo:
            return PowerResult(modcod, self.lo, True, ber_lo, eff)   # meets even at minimum power
        lo, hi, ber = self.lo, self.hi, ber_hi
        for _ in range(iters):                                       # crossover is in (lo, hi]
            mid = 0.5 * (lo + hi)
            m, b = self._probe(modcod, mid)
            if m:
                hi, ber = mid, b
            else:
                lo = mid
        return PowerResult(modcod, round(hi, 2), True, ber, eff)

    def adapt(self, ladder) -> PowerResult | None:
        """Highest-efficiency modcod feasible within the power budget, at its minimum power (ties →
        lower power). ``None`` if nothing is feasible — power alone cannot; avoid or hop instead."""
        best = None
        for mc in ladder:
            res = self.min_power(mc)
            if res.feasible:
                key = (res.efficiency, -res.min_power_db)
                if best is None or key > (best.efficiency, -best.min_power_db):
                    best = res
        return best
