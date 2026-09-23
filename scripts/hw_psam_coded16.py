#!/usr/bin/env python3
"""Verify PSAM fixes coded 16-QAM on hardware.

The bench characterized a limit: at the marginal SNRs where coding matters, the blind decision-directed
carrier loop slips to a 90°-rotated alias of the 16-QAM constellation ~half the time, permuting every
symbol's bits so Viterbi decodes garbage. Pilot-symbol-aided modulation (PSAM, `pilot_spacing > 0`)
measures the carrier phase from known pilots and interpolates — rotation-invariant, so no slip.

This runs coded 16-QAM (`conv_k3_r12`) N times at a marginal attenuation, first with the blind DD
loop (`pilot_spacing=0`) and then with PSAM, and reports the per-trial info-BER and the slip rate
(fraction of trials that decode to garbage). PSAM should be consistently clean where DD is ~50/50.

Wiring: the two-Pluto cabled path (no HackRF). Ends with os._exit.

Run:  python scripts/hw_psam_coded16.py --tx-atten 53 --trials 8
      python scripts/hw_psam_coded16.py --tx-atten 55 --spacing 16
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.scoring.metrics import compute_metrics


def _trial(be, seed, bits):
    r = be.run_link(LinkParams(modulation="16qam", coding="conv_k3_r12", n_payload_bits=bits,
                               sps=8, rolloff=0.35, seed=seed))
    m = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
    return m.ber, r.meta.get("carrier", "?")


def _run(be, label, trials, bits, target):
    bers = []
    print(f"  {label}:")
    for i in range(trials):
        ber, carrier = _trial(be, seed=100 + i, bits=bits)
        bers.append(ber)
        tag = "clean" if ber <= target else "SLIP/garbage"
        print(f"    trial {i}: info-BER={ber:.2e}  [{tag}]  ({carrier})", flush=True)
    bers = np.array(bers)
    clean = int((bers <= target).sum())
    print(f"    => {clean}/{trials} clean (BER<= {target:g}); median BER {np.median(bers):.2e}\n",
          flush=True)
    return clean, trials


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--tx-atten", type=float, default=53.0, help="marginal SNR (dB) where coding matters")
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--bits", type=int, default=8000)
    ap.add_argument("--spacing", type=int, default=16, help="PSAM pilot spacing (payload syms/pilot)")
    ap.add_argument("--target-ber", type=float, default=1e-2)
    args = ap.parse_args()

    try:
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import PlutoBackend ({exc})", file=sys.stderr)
        return 2

    print(f"Coded 16-QAM on hardware — blind DD vs PSAM  (tx_atten={args.tx_atten:g} dB, "
          f"{args.trials} trials each)\n")

    # Blind DD loop (pilot_spacing=0): the Result-8 baseline.
    be0 = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0,
                       rx_gain_db=args.rx_gain, sync_mode="zc", pilot_spacing=0)
    be0.set_condition(tx_atten_db=args.tx_atten)
    dd_clean, n = _run(be0, "blind DD loop (pilot_spacing=0)", args.trials, args.bits, args.target_ber)
    be0.close()

    # PSAM (pilot_spacing>0): rotation-invariant.
    be1 = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0,
                       rx_gain_db=args.rx_gain, sync_mode="zc", pilot_spacing=args.spacing)
    be1.set_condition(tx_atten_db=args.tx_atten)
    psam_clean, _ = _run(be1, f"PSAM (pilot every {args.spacing} syms)", args.trials, args.bits,
                         args.target_ber)
    be1.close()

    print(f"=> blind DD: {dd_clean}/{n} clean   PSAM: {psam_clean}/{n} clean")
    if psam_clean > dd_clean:
        print("   PSAM removes the 90° carrier slip — coded 16-QAM is now robust at marginal SNR.")
    elif psam_clean == n == dd_clean:
        print("   both clean at this attenuation — raise --tx-atten toward the slip regime to see it.")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
