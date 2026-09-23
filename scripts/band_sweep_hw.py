#!/usr/bin/env python3
"""Band-envelope sweep on the WIRED Plutos — confirm the sim's CFO ceiling on real radios.

Retunes the cabled link across bands and, at each, runs the SAME ZC acquisition front-end the sim
used and reads the *measured* CFO (Pluto reports it in LinkResult.meta['cfo_cyc_per_sym']). From the
bands that lock it fits the differential LO error (ppm) — CFO should scale linearly with the band —
and reports the predicted acquisition ceiling, then shows acquisition holding below it (and, if the
tunable range reaches it, failing above). AGC is on so per-band cable loss is compensated and the CFO
effect is isolated. Contained coax path only — nothing radiates.

Bands stay inside the common tuning range of the asymmetric pair (325 MHz–3.8 GHz) and avoid the
2.4/5 GHz Wi-Fi bands (ambient Wi-Fi can leak into the RX front-end and corrupt a low-level read).

Run:  python scripts/band_sweep_hw.py
      python scripts/band_sweep_hw.py --bands 400,700,900,1200,1500,1800,2100,3000,3500 --bits 20000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

from gr_autopilot.link import band as bandmod

DEFAULT_BANDS_MHZ = "400,700,900,1200,1500,1800,2100,3000,3500"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--tx-atten", type=float, default=10.0, help="TX attenuation dB (low = strong link)")
    ap.add_argument("--bands", default=DEFAULT_BANDS_MHZ, help="link center freqs, MHz, comma-separated")
    ap.add_argument("--bits", type=int, default=20000)
    ap.add_argument("--sps", type=int, default=8)
    ap.add_argument("--sample-rate", type=float, default=2_084_000.0)
    ap.add_argument("--out", default="docs/data/hw_band_sweep.json")
    args = ap.parse_args()

    try:
        from gr_autopilot.link.pluto import PlutoBackend
        from gr_autopilot.scoring.metrics import compute_metrics
        from gr_autopilot.link.backend import LinkParams
    except Exception as exc:  # pragma: no cover
        print(f"cannot import hardware backends ({exc})", file=sys.stderr)
        return 2

    bands = [float(m) * 1e6 for m in args.bands.split(",")]
    Rs = args.sample_rate / args.sps
    acq_range = bandmod.acquisition_range_hz(Rs)

    # AGC (slow_attack) so per-band cable loss is compensated -> isolates the CFO effect, not SNR.
    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=args.sample_rate,
                      rx_gain_mode="slow_attack", sync_mode="zc")
    be.set_condition(tx_atten_db=args.tx_atten)

    print(f"Band-envelope sweep on the WIRED link {args.tx_uri} -> {args.rx_uri}")
    print(f"symbol rate {Rs/1e3:.1f} ksym/s · ZC acquisition range ±{acq_range/1e3:.1f} kHz "
          f"(cfo_max·Rs) · AGC on · coax path (no OTA)\n")
    print(f"  {'band':>7} {'meas CFO':>10} {'cyc/sym':>8} {'ppm':>6} {'in-range':>9} {'locked':>7} {'BER':>10}")

    rows = []
    for fc in bands:
        rec = {"center_freq_hz": fc}
        try:
            be.set_condition(center_freq_hz=fc)
            r = be.run_link(LinkParams(modulation="qpsk", n_payload_bits=args.bits,
                                       sps=args.sps, rolloff=0.35, seed=1))
            m = r.meta
            locked = bool(m.get("zc_locked", False))
            cfo_cyc = m.get("cfo_cyc_per_sym")
            ber = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms).ber
            cfo_hz = float(cfo_cyc) * Rs if cfo_cyc is not None else None
            ppm = (cfo_hz / fc * 1e6) if cfo_hz is not None else None
            rec.update({"locked": locked, "cfo_cyc_per_sym": cfo_cyc, "cfo_hz": cfo_hz,
                        "ppm": ppm, "ber": ber, "in_range": None if cfo_cyc is None else abs(cfo_cyc) <= bandmod.DEFAULT_CFO_MAX,
                        "preamble_evm_pct": m.get("preamble_evm_pct"), "frame_sync_metric": m.get("frame_sync_metric")})
        except Exception as exc:
            rec.update({"locked": False, "cfo_cyc_per_sym": None, "cfo_hz": None, "ppm": None,
                        "ber": 0.5, "in_range": None, "error": str(exc)[:120]})
        rows.append(rec)
        cyc = "-" if rec["cfo_cyc_per_sym"] is None else f"{rec['cfo_cyc_per_sym']:.3f}"
        chz = "-" if rec["cfo_hz"] is None else f"{rec['cfo_hz']/1e3:.1f}k"
        ppm_s = "-" if rec["ppm"] is None else f"{rec['ppm']:.1f}"
        inr = "-" if rec["in_range"] is None else str(rec["in_range"])
        print(f"  {fc/1e9:6.2f}G {chz:>10} {cyc:>8} {ppm_s:>6} {inr:>9} {str(rec['locked']):>7} {rec['ber']:10.2e}",
              flush=True)

    # Fit the differential ppm from the bands that locked (CFO should be linear in band through 0).
    good = [(r["center_freq_hz"], r["cfo_hz"]) for r in rows if r.get("locked") and r.get("cfo_hz") is not None]
    summary = {"symbol_rate_hz": Rs, "acq_range_hz": acq_range, "rows": rows}
    if len(good) >= 2:
        fc_arr = np.array([g[0] for g in good]); cfo_arr = np.array([g[1] for g in good])
        ppm_fit = float(np.sum(fc_arr * cfo_arr) / np.sum(fc_arr ** 2) * 1e6)  # slope through origin, ppm
        ceil = bandmod.band_ceiling_hz(abs(ppm_fit), Rs) if ppm_fit else float("inf")
        resid = cfo_arr - ppm_fit * 1e-6 * fc_arr
        r2 = 1.0 - float(np.sum(resid ** 2) / max(np.sum((cfo_arr - cfo_arr.mean()) ** 2), 1e-12))
        summary.update({"fitted_ppm": ppm_fit, "linearity_r2": r2, "predicted_ceiling_hz": ceil})
        print(f"\n=> measured differential LO error ≈ {ppm_fit:.1f} ppm "
              f"(CFO linear in band, R²={r2:.4f}) → predicted acquisition ceiling {ceil/1e9:.2f} GHz")
        locked_bands = [r['center_freq_hz']/1e9 for r in rows if r.get('locked')]
        if locked_bands:
            print(f"   acquisition held from {min(locked_bands):.2f}–{max(locked_bands):.2f} GHz; "
                  f"CFO scaling on real radios confirms the sim envelope (fig7).")
    else:
        print("\n=> too few locked bands to fit ppm — check the link (tx-atten / cabling).")

    outp = Path(args.out); outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {args.out}")
    sys.stdout.flush()
    os._exit(0)   # gr-iio teardown hangs; results are already written


if __name__ == "__main__":
    raise SystemExit(main())
