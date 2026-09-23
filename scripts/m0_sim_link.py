#!/usr/bin/env python3
"""M0 (in simulation): run the QPSK link end-to-end with no hardware and confirm scoring.

Two backends, same loop:
  1) NumpySimBackend  — exact AWGN; BER printed against closed-form theory.
  2) GrSimBackend     — a real GNU Radio flowgraph through channels.channel_model;
                        BER + measured SNR across a noise sweep (clean -> ~0, noisy -> up).

Run: python scripts/m0_sim_link.py
"""
from __future__ import annotations

import math

from scipy.special import erfc

from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.scoring.metrics import compute_metrics


def qfunc(x: float) -> float:
    return 0.5 * erfc(x / math.sqrt(2.0))


def theory_ber(mod: str, eb_n0_db: float) -> float:
    ebn0 = 10.0 ** (eb_n0_db / 10.0)
    if mod in ("bpsk", "qpsk"):  # Gray, same per-bit BER
        return qfunc(math.sqrt(2.0 * ebn0))
    if mod == "16qam":  # Gray 16-QAM approximation
        return 0.75 * qfunc(math.sqrt(0.8 * ebn0))
    raise ValueError(mod)


def bits_per_symbol(mod: str) -> int:
    return {"bpsk": 1, "qpsk": 2, "16qam": 4}[mod]


def run_numpy(mod: str = "qpsk", n_bits: int = 2_000_000) -> None:
    print(f"\n=== NumpySimBackend  ({mod.upper()}, {n_bits:,} bits) — BER vs theory ===")
    print(f"{'Es/N0':>6} {'Eb/N0':>6} {'measured BER':>14} {'theory BER':>12} {'EVM%':>7} {'SNR dB':>7}")
    be = NumpySimBackend()
    bps = bits_per_symbol(mod)
    for eb_n0 in (2.0, 4.0, 6.0, 8.0):
        es_n0 = eb_n0 + 10.0 * math.log10(bps)
        r = be.run_link(LinkParams(modulation=mod, n_payload_bits=n_bits, es_n0_db=es_n0))
        m = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
        print(f"{es_n0:6.1f} {eb_n0:6.1f} {m.ber:14.3e} {theory_ber(mod, eb_n0):12.3e} "
              f"{m.evm_pct:7.2f} {m.snr_db:7.2f}")


def run_gr(mod: str = "qpsk", n_bits: int = 200_000) -> None:
    try:
        from gr_autopilot.link.gr_sim import GrSimBackend
    except Exception as exc:  # pragma: no cover
        print(f"\n=== GrSimBackend skipped (GNU Radio unavailable: {exc}) ===")
        return
    print(f"\n=== GrSimBackend  ({mod.upper()}, {n_bits:,} bits, sps=4) — noise sweep ===")
    print(f"{'noise_v':>8} {'measured BER':>14} {'SNR dB':>7} {'EVM%':>7} {'offset':>7}")
    be = GrSimBackend()
    for nv in (0.0, 0.1, 0.25, 0.5, 0.8, 1.2):
        r = be.run_link(LinkParams(modulation=mod, n_payload_bits=n_bits, sps=4, noise_voltage=nv))
        m = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
        print(f"{nv:8.2f} {m.ber:14.3e} {m.snr_db:7.2f} {m.evm_pct:7.2f} {r.meta['align_offset']:7d}")


if __name__ == "__main__":
    for mod in ("bpsk", "qpsk", "16qam"):
        run_numpy(mod)
    run_gr("qpsk")
