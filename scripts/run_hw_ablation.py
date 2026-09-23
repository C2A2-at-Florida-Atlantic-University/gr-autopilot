#!/usr/bin/env python3
"""Ablation on HARDWARE: measurement-guided outer loop vs the exhaustive two-loop controller,
over the real cabled PlutoSDR link (spec C4, sample efficiency).

Same question as scripts/llm_ablation.py (sim), now in wall-clock seconds on the bench: does
probing top-down and stopping at the first rung that meets the target reach the AMC boundary in
fewer *physical* link trials than climbing every rung?

Both arms share the SAME inner loop (BO over the receiver phase correction) and the SAME framework
grader; only the outer search differs — exactly as in the sim ablation, now on real radios in
wall-clock seconds:
  * climb  — BO-tune every rung (bpsk, qpsk, 16qam), then keep the best (exhaustive; ~39 runs).
  * guided — probe top-down, keep a rung that already meets, BO-tune only a rung that fails within
             a small factor of target (marginal -> plausibly a tuning problem), skip a rung that is
             hopelessly far (SNR-limited), and stop at the first rung that meets.
Neither arm is told the attenuation or the SNR. Each link run is a real TX/RX capture + ZC decode;
we report link runs AND bench wall-clock seconds.

The exhaustive climb evaluates every rung, so its BO-tuned trajectory *is* the measured ground
truth; the guided arm is scored against it. Ends with os._exit (gr-iio teardown hangs).

Run:  python scripts/run_hw_ablation.py
      python scripts/run_hw_ablation.py --attens 35,52,57 --rx-gain 40 --confirm-bits 8000
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

from gr_autopilot.control import TwoLoopController
from gr_autopilot.control.objective import spectral_efficiency
from gr_autopilot.flowgraph import FlowgraphSpec, build_and_run
from gr_autopilot.scoring.metrics import compute_metrics

LADDER = ("bpsk", "qpsk", "16qam")
# The shared inner loop: BO over the receiver phase correction (both arms use it identically).
KNOBS = {"phase_correction_rad": (-math.pi / 4, math.pi / 4)}
RESCUE_FACTOR = 4.0  # a raw-failing rung within this factor of target is worth a BO rescue attempt


def _measure(be, mod, bits, seed=0):
    r = build_and_run(FlowgraphSpec.link(mod, sps=8, rolloff=0.35), be, n_payload_bits=bits, seed=seed)
    return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)


def _bo_tune_rung(cb, mod, target, bits):
    """Run the same BO inner loop the controller uses, on a single rung. Returns its best BER."""
    res = TwoLoopController(cb, target_ber=target, ladder=(mod,), knobs=KNOBS,
                            confirm_bits=bits, sps=8, rolloff=0.35).run({})
    return res.per_mod[mod].ber


def run_two_loop(be, cb_cls, atten, target, bits):
    """Exhaustive climb: BO-tune ALL 3 rungs, then keep the best. Returns (chosen, runs, s, traj)."""
    be.set_condition(tx_atten_db=atten)
    cb = cb_cls(be)
    t0 = time.time()
    res = TwoLoopController(cb, target_ber=target, ladder=LADDER, knobs=KNOBS,
                            confirm_bits=bits, sps=8, rolloff=0.35).run({})
    return res.chosen, cb.runs, time.time() - t0, res.trajectory


def run_guided(be, cb_cls, atten, target, bits):
    """Top-down, same BO inner loop as the climb but SELECTIVE: probe a rung raw; keep it if it
    already meets; BO-tune it only if it fails within RESCUE_FACTOR of target (marginal, plausibly
    a tuning problem); skip to the next rung down if it is hopelessly far (SNR-limited). Stop at
    the first rung that meets. Same grader, same knobs -- only the search differs from the climb."""
    be.set_condition(tx_atten_db=atten)
    cb = cb_cls(be)
    t0 = time.time()
    steps = []
    for mod in ("16qam", "qpsk", "bpsk"):
        m = _measure(cb, mod, bits)
        if m.ber <= target:
            steps.append(f"probe {mod}: BER={m.ber:.2e} meets")
            return mod, cb.runs, time.time() - t0, steps
        if m.ber <= RESCUE_FACTOR * target:
            steps.append(f"probe {mod}: BER={m.ber:.2e} marginal -> BO rescue")
            if _bo_tune_rung(cb, mod, target, bits) <= target:
                steps.append(f"  BO rescued {mod}")
                return mod, cb.runs, time.time() - t0, steps
        else:
            steps.append(f"probe {mod}: BER={m.ber:.2e} far -> drop")
    return LADDER[0], cb.runs, time.time() - t0, steps


def _truth_from_trajectory(trajectory, target) -> str:
    meeting = [t["modulation"] for t in trajectory if t["ber"] <= target]
    return max(meeting, key=spectral_efficiency) if meeting else LADDER[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--attens", default="35,52,57",
                    help="hidden TX attenuations (dB), one operating point each (high→low SNR)")
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--target-ber", type=float, default=1e-2)
    ap.add_argument("--confirm-bits", type=int, default=8000)
    args = ap.parse_args()

    try:
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import PlutoBackend ({exc}); need GNU Radio + gr-iio", file=sys.stderr)
        return 2
    from scripts.llm_ablation import CountingBackend

    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0,
                      rx_gain_db=args.rx_gain, sync_mode="zc")
    attens = [float(a) for a in args.attens.split(",")]

    print(f"Hardware ablation  {args.tx_uri} -> {args.rx_uri}  target BER <= {args.target_ber:g}")
    print("exhaustive two-loop climb  vs  measurement-guided top-down early-stop  "
          "(attenuation/SNR withheld)\n", flush=True)

    rows = []
    for atten in attens:
        print(f"--- TX atten {atten:.0f} dB ---", flush=True)
        tl_choice, tl_runs, tl_s, traj = run_two_loop(be, CountingBackend, atten, args.target_ber,
                                                       args.confirm_bits)
        truth = _truth_from_trajectory(traj, args.target_ber)
        for t in traj:
            meet = "meets" if t["ber"] <= args.target_ber else "FAILS"
            print(f"    climb {t['modulation']:>6}: BER={t['ber']:.2e}  [{meet}]", flush=True)
        g_choice, g_runs, g_s, steps = run_guided(be, CountingBackend, atten, args.target_ber,
                                                  args.confirm_bits)
        for s in steps:
            print(f"    guided {s}", flush=True)
        print(f"  => climb: {tl_choice.upper()} in {tl_runs} runs / {tl_s:.1f}s   |   "
              f"guided: {g_choice.upper()} in {g_runs} runs / {g_s:.1f}s   "
              f"(truth {truth.upper()})\n", flush=True)
        rows.append((atten, truth, tl_choice, tl_runs, tl_s, g_choice, g_runs, g_s))

    # summary
    n = len(rows)
    tl_ok = sum(r[2] == r[1] for r in rows) / n
    g_ok = sum(r[5] == r[1] for r in rows) / n
    tl_runs = sum(r[3] for r in rows)
    g_runs = sum(r[6] for r in rows)
    tl_s = sum(r[4] for r in rows)
    g_s = sum(r[7] for r in rows)
    print("=" * 64)
    print(f"{'atten':>5} | {'truth':>6} | {'climb':>6} {'runs':>4} {'sec':>6} | {'guided':>6} {'runs':>4} {'sec':>6}")
    for atten, truth, tlc, tlr, tls, gc, gr, gs in rows:
        print(f"{atten:>5.0f} | {truth:>6} | {tlc:>6} {tlr:>4} {tls:>6.1f} | {gc:>6} {gr:>4} {gs:>6.1f}")
    print("-" * 64)
    print(f"accuracy vs measured AMC boundary : climb {tl_ok:.0%}   guided {g_ok:.0%}")
    print(f"total physical link runs          : climb {tl_runs}   guided {g_runs}  "
          f"({tl_runs / max(g_runs, 1):.1f}x)")
    print(f"total bench wall-clock (seconds)  : climb {tl_s:.1f}   guided {g_s:.1f}  "
          f"({tl_s / max(g_s, 1e-9):.1f}x faster)")

    sys.stdout.flush()
    os._exit(0)  # gr-iio teardown hangs; exit hard after printing


if __name__ == "__main__":
    raise SystemExit(main())
