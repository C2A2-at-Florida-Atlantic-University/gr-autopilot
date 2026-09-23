#!/usr/bin/env python3
"""EXPLORATORY: two-loop optimization on hardware where the INNER Bayesian-optimization loop
tunes the RRC rolloff (occupied bandwidth) to the tightest pulse still meeting BER, so
bits/s/Hz = bits_per_symbol / (1 + rolloff).

Caveat, measured on the coaxial bench: on this link the rolloff has a sharp,
per-modulation BER cliff, and "minimize bandwidth" drives the search straight at it. With a
small trial budget the BO is unreliable here -- it can push a *feasible* modcod below its cliff
and get it wrongly excluded, mis-selecting the modulation. This script is kept as the
exploration that produced that negative result; the robust hardware two-loop result is the
modulation-selection AMC crossover (scripts/run_hw_amc.py), and the BO inner loop's sample
efficiency is shown on well-behaved objectives by scripts/bo_ablation.py.

Run:  python scripts/run_hw_spectral.py --atten 45
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from gr_autopilot.control import TwoLoopController
from gr_autopilot.control.objective import bandwidth_loss, bits_per_hz
from gr_autopilot.ledger import EditLedger


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--atten", type=float, default=45.0, help="hidden TX attenuation (dB)")
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--bo-budget", type=int, default=8)
    args = ap.parse_args()

    try:
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import PlutoBackend ({exc})", file=sys.stderr)
        return 2

    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0)
    be.set_condition(tx_atten_db=args.atten)
    target = args.target_ber
    led = EditLedger(Path(tempfile.mkdtemp()) / "spectral.jsonl")
    ctrl = TwoLoopController(
        be, target_ber=target, ladder=("bpsk", "qpsk", "16qam"),
        knobs={"rolloff": (0.25, 0.6)},                       # search band (below ~0.3 decoding fails)
        # BER margin (target/3) so the BO settles on a reliably-feasible rolloff, not the noisy
        # cliff edge -- mitigates but does not fully fix the fragility (see module docstring).
        inner_loss=lambda m, kv: bandwidth_loss(m, kv["rolloff"], target / 3.0),
        bo_budget=args.bo_budget, trial_bits=6000, confirm_bits=12000,
        ledger=led, seed=0, sps=8,
    )
    res = ctrl.run({})

    print(f"two-loop spectral optimization on hardware  (TX atten {args.atten:.0f} dB, SNR hidden; "
          f"target BER <= {target:g})")
    print(f"{'mod':>6}  {'best rolloff':>12}  {'BER@best':>9}  {'bits/s/Hz':>9}")
    for mod in ("bpsk", "qpsk", "16qam"):
        roll = res.best_knobs[mod].get("rolloff", float("nan"))
        m = res.per_mod[mod]
        se = bits_per_hz(mod, roll) if m.ber <= target else 0.0
        print(f"{mod:>6}  {roll:>12.3f}  {m.ber:>9.2e}  {se:>9.3f}")
    chosen_roll = res.best_knobs[res.chosen]["rolloff"]
    print(f"\n=> chose {res.chosen.upper()} at rolloff {chosen_roll:.3f}  "
          f"-> {bits_per_hz(res.chosen, chosen_roll):.3f} bits/s/Hz  "
          f"(BER {res.per_mod[res.chosen].ber:.2e}), {res.iterations} iterations")
    print("\nedit ledger:")
    print("  " + led.compact_table().replace("\n", "\n  "))
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
