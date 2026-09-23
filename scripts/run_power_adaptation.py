#!/usr/bin/env python3
"""Power adaptation — the fifth countermeasure (sim): transmit no louder than necessary, spend power
for rate, boost to overcome a jammer — all bounded by a finite power budget.

The agent owns a TX-power knob but is not told the path loss, so it finds the power it needs by
measurement on the grader. ``PowerController.adapt`` returns the highest-efficiency modcod feasible
within the budget, at its MINIMUM power:

  A strong link   -> hold 16-QAM but back OFF (least energy / interference footprint);
  B marginal link -> BOOST to hold 16-QAM instead of dropping the rate (power buys rate);
  C weak jammer   -> boost to overcome it (up to the rate the budget affords);
  D strong jammer -> over budget: power alone cannot, hand off to avoidance/hopping.

Run: python scripts/run_power_adaptation.py
"""
from __future__ import annotations

import sys

from gr_autopilot.control.power import PowerController
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend

LADDER = ["bpsk", "qpsk", "16qam"]


def scenario(label, note, be) -> None:
    r = PowerController(be, target_ber=1e-2, bits=40_000).adapt(LADDER)
    print(f"[{label}] {note}")
    if r is None:
        print("   => no rung feasible within the power budget — power alone can't; avoid or hop.\n")
        return
    verb = f"backs OFF {abs(r.min_power_db):.0f} dB" if r.min_power_db < 0 else f"BOOSTS {r.min_power_db:.0f} dB"
    print(f"   => hold {r.modcod.upper()} ({r.efficiency:g} b/sym) at tx_power {r.min_power_db:+.1f} dB "
          f"({verb}), BER {r.ber:.1e}\n")


def main() -> int:
    print("Power adaptation — the fifth countermeasure (path loss hidden; power found by measurement)\n")
    scenario("A", "strong link, no jammer — efficiency",
             InterferenceSimBackend(clean_es_n0_db=25.0))
    scenario("B", "marginal link — power vs rate (hold 16-QAM by spending power)",
             InterferenceSimBackend(clean_es_n0_db=10.0))
    scenario("C", "a weak jammer — boost SINR to overcome it",
             InterferenceSimBackend(clean_es_n0_db=18.0, interferer=Interferer(2.4e9, inr_db=12.0)))
    scenario("D", "a strong jammer — the boost needed exceeds the budget",
             InterferenceSimBackend(clean_es_n0_db=18.0, interferer=Interferer(2.4e9, inr_db=30.0)))
    print("=> power is a fifth countermeasure alongside AMC / coding / avoidance / hopping: efficiency\n"
          "   when strong, rate when marginal, a boost vs a weak jammer — bounded by the PA budget.")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
