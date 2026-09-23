#!/usr/bin/env python3
"""Live telemetry dashboard demo (sim, no radios).

Serves the read-only dashboard and drives a scripted-but-real narrative through it: a clean AMC
climb, then a band-limited noise jammer appears, then the agent plays the MONITOR ROLE — goes
silent, energy-detects the candidate band (the occupancy panel lights up), and hops past the
jammer's footprint to a sensed-clear channel where AMC restores 16-QAM. Metrics, constellations,
and the occupancy sweep are REAL numpy-sim measurements (InterferenceSimBackend.sense_spectrum);
the decision *sequence* mirrors the avoidance controller; the PSD is a representative capture
(pulse-shaped link + the jammer's noise hump). Open the printed URL in a browser and watch.

Run:  python scripts/run_dashboard_demo.py           # then open http://127.0.0.1:8080
      python scripts/run_dashboard_demo.py --port 9000 --once   # write one pass and exit
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import numpy as np

from gr_autopilot import modulation
from gr_autopilot.control.objective import parse_modcod
from gr_autopilot.ledger import EditLedger, LedgerEntry
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.link.sync import rrc_taps
from gr_autopilot.scoring.metrics import compute_metrics
from gr_autopilot.telemetry import TelemetryWriter
from gr_autopilot.telemetry.server import serve

SAMPLE_RATE = 2_084_000.0
GOAL = "meet BER ≤ 1e-2, maximize spectral efficiency — link under interference"


def viz_capture(link_freq, interferer, rng, n=4096, sps=8, rolloff=0.35, sig_snr_db=22.0):
    """A representative baseband capture: pulse-shaped link at center + the jammer tone at its
    offset from the link (so on-channel it sits on the signal, and after avoidance it moves to
    the band edge). For the spectrum panel only."""
    nsym = n // sps + 16
    syms = modulation.get("qpsk").modulate(rng.integers(0, 2, nsym * 2))
    up = np.zeros(syms.size * sps, dtype=complex)
    up[::sps] = syms
    sig = np.convolve(up, rrc_taps(sps, rolloff))[:n]
    sig /= np.sqrt(np.mean(np.abs(sig) ** 2)) + 1e-12
    sigma = 10 ** (-sig_snr_db / 20.0)
    cap = sig + sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2)
    if interferer is not None:
        off = (interferer.center_freq_hz - link_freq) / SAMPLE_RATE  # cycles/sample
        if abs(off) < 0.5:  # jammer lands inside the captured band -> render it
            if getattr(interferer, "kind", "cw") == "noise" and interferer.bandwidth_hz > 0:
                jb = rng.standard_normal(n) + 1j * rng.standard_normal(n)
                J = np.fft.fft(jb)
                fr = np.fft.fftfreq(n)  # cycles/sample
                half = min(0.49, interferer.bandwidth_hz / SAMPLE_RATE / 2.0)
                J[np.abs(fr - off) > half] = 0.0   # band-limit around the jammer offset
                jam = np.fft.ifft(J)
                cap = cap + 2.4 / (np.std(jam) + 1e-12) * jam  # noise hump lifts the floor in-band
            else:
                cap = cap + 3.0 * np.exp(1j * 2 * np.pi * off * np.arange(n))  # CW spike
    return cap


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--once", action="store_true", help="write one pass and exit (for testing)")
    ap.add_argument("--record", default="", help="write the whole narrative (all frames) to JSON and exit")
    ap.add_argument("--pause", type=float, default=1.4)
    args = ap.parse_args()
    single = args.once or bool(args.record)

    d = Path(tempfile.mkdtemp(prefix="gr_autopilot_dash_"))
    ledger_path, snap_path = d / "ledger.jsonl", d / "telemetry.json"
    led, tw = EditLedger(ledger_path), TelemetryWriter(snap_path)
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=None, center_freq_hz=2.400e9)
    rng = np.random.default_rng(0)
    it = [0]
    frames: list[dict] = []

    if not single:
        serve(str(ledger_path), str(snap_path), port=args.port, background=True)
        print(f"telemetry dashboard live:  http://127.0.0.1:{args.port}\n(Ctrl-C to stop)\n")

    def step(modcod, freq, loop, detail, edit, verdict, jam=None, occ=None):
        it[0] += 1
        be.set_condition(center_freq_hz=freq, interferer=jam)
        mod, coding = parse_modcod(modcod)
        r = be.run_link(LinkParams(modulation=mod, coding=coding, n_payload_bits=8000,
                                   sps=8, rolloff=0.35, seed=it[0]))
        m = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
        led.append(LedgerEntry(iteration=it[0], structure_id=f"{modcod}@{freq/1e6:.3f}",
                               edit_description=edit,
                               metrics={"BER": m.ber, "EVM": round(m.evm_pct, 2),
                                        "SNR_est": round(m.snr_db, 2)},
                               verdict=verdict, loop=loop))
        snap = tw.write(result=r, metrics=m, target_ber=1e-2,
                        structure={"modcod": modcod, "center_freq_hz": freq},
                        active_loop={"loop": loop, "detail": detail},
                        rx_capture=viz_capture(freq, jam, rng), goal=GOAL, occupancy=occ)
        if args.record:
            frames.append({"snapshot": snap, "ledger": led.read()})
        if not single:
            print(f"  [{it[0]:>2}] {loop:>5} {modcod:>6} @ {freq/1e6:.3f} MHz  "
                  f"BER={m.ber:.2e}  {verdict}", flush=True)
            time.sleep(args.pause)

    F0, F_CLEAR = 2.400e9, 2.405e9
    candidates = [2.400e9, 2.401e9, 2.405e9, 2.410e9, 2.420e9]
    # A band-limited noise jammer (2 MHz) centered on the link — mirrors the Stage-2 hardware run:
    # the monitor sees it span TWO channels (2400/2401) and the agent hops past the whole footprint.
    jam = Interferer(center_freq_hz=F0, inr_db=40.0, kind="noise", bandwidth_hz=2.0e6)

    def sense():
        """The monitor's real energy-detection sweep across the candidate band (link silent)."""
        be.set_condition(interferer=jam)
        return be.sense_spectrum(candidates)

    passes = 1 if single else 10_000
    for _ in range(passes):
        # A — clean AMC climb at 2.400 GHz (no interferer; occupancy panel absent)
        step("bpsk", F0, "outer", "try BPSK", "structure: BPSK", "tuning")
        step("qpsk", F0, "outer", "try QPSK", "structure: QPSK", "tuning")
        for k in (1, 2, 3):  # B — inner BO loop tuning 16-QAM
            step("16qam", F0, "inner", f"trial {k}/3", "BO: tune phase correction", "tuning")
        step("16qam", F0, "outer", "select", "keep 16-QAM (max efficiency, meets BER)", "kept")
        # C — a jammer appears; the 16-QAM link sitting on 2.400 collapses
        occ = sense()
        step("16qam", F0, "outer", "re-measure", "interference — BER spikes", "reverted", jam, occ)
        # D — MONITOR ROLE: go silent, energy-detect the band -> occupancy map (the new panel)
        step("bpsk", F0, "outer", "monitor sweep",
             "sense the band (silent): 2400/2401 occupied, 2405+ clear", "sensing", jam, occ)
        # E — hop past the jammer's footprint to the sensed-clear channel; AMC restores 16-QAM
        step("qpsk", F_CLEAR, "outer", "retune 2400→2405", "sensed-clear → hop to 2405.0", "tuning", jam, occ)
        step("16qam", F_CLEAR, "outer", "select", "keep 16-QAM @ 2405 — link restored", "kept", jam, occ)
        if single:
            break

    if args.record:
        Path(args.record).write_text(json.dumps({"frames": frames}))
        print(f"wrote recording ({len(frames)} frames) to {args.record}")
    elif args.once:
        print(f"wrote {snap_path} and {ledger_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
