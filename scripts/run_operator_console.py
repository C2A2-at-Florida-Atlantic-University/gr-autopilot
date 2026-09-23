#!/usr/bin/env python3
"""Operator console — drive the AMC loop from the browser.

Serves the telemetry dashboard WITH operator controls (the dashboard's one write surface, POST
/control): start / pause, reset, target BER, the hidden SNR (a slider), and a co-channel jammer
toggle. The AGENT still runs its own structural loop — climb the modulation ladder, keep the
highest rung that meets BER — the operator just sets the scenario and watches it adapt in real
time. Sim only (InterferenceSimBackend); no radios.

This is the OPERATOR surface, deliberately distinct from the AGENT's control (which stays on the
MCP/chat surface). Open the printed URL and drive it.

Run:  python scripts/run_operator_console.py         # then open http://127.0.0.1:8080
"""
from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

from gr_autopilot.control.objective import spectral_efficiency
from gr_autopilot.ledger import EditLedger, LedgerEntry
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.scoring.metrics import compute_metrics
from gr_autopilot.telemetry import TelemetryWriter
from gr_autopilot.telemetry.server import ControlState, serve

LADDER = ("bpsk", "qpsk", "16qam")
BPS = {"bpsk": 1, "qpsk": 2, "16qam": 4}
GOAL = "operator sets the scenario; the agent keeps the highest-efficiency modcod meeting BER"


def amc_step(be, target_ber, seed):
    """One AMC climb: keep the highest-efficiency rung meeting the target, else the robust rung."""
    res = {}
    for mod in LADDER:
        r = be.run_link(LinkParams(modulation=mod, n_payload_bits=8000, sps=8, rolloff=0.35, seed=seed))
        res[mod] = (r, compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms))
    meeting = [m for m in LADDER if res[m][1].ber <= target_ber]
    chosen = max(meeting, key=spectral_efficiency) if meeting else LADDER[0]
    return chosen, res[chosen][0], res[chosen][1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between adaptation cycles")
    ap.add_argument("--es-n0-db", type=float, default=14.0, help="initial hidden SNR (operator-adjustable)")
    ap.add_argument("--jammer-inr-db", type=float, default=8.0, help="jammer strength when toggled on")
    args = ap.parse_args()

    d = Path(tempfile.mkdtemp(prefix="gr_autopilot_ops_"))
    ledger_path, snap_path = d / "ledger.jsonl", d / "telemetry.json"
    control = ControlState(es_n0_db=args.es_n0_db, target_ber=1e-2, running=True, jammer=False)
    led = EditLedger(ledger_path)
    tw = TelemetryWriter(snap_path)
    be = InterferenceSimBackend(clean_es_n0_db=args.es_n0_db, interferer=None, center_freq_hz=2.4e9)

    serve(str(ledger_path), str(snap_path), port=args.port, background=True, control=control)
    print(f"operator console live:  http://127.0.0.1:{args.port}")
    print("drive it from the browser — start/pause, reset, target BER, SNR slider, jammer toggle "
          "(Ctrl-C to stop)\n")

    it = 0
    while True:
        if control.take_reset():
            ledger_path.write_text("")           # clear the timeline
            led = EditLedger(ledger_path)
            it = 0
        c = control.get()
        if not c.get("running", True):
            time.sleep(0.25)                     # paused — hold the current snapshot
            continue
        it += 1
        es_n0 = c["es_n0_db"] if c.get("es_n0_db") is not None else args.es_n0_db
        target = c.get("target_ber", 1e-2)
        jammer = bool(c.get("jammer", False))
        be.set_condition(
            clean_es_n0_db=es_n0,
            interferer=Interferer(center_freq_hz=2.4e9, inr_db=args.jammer_inr_db, kind="cw")
            if jammer else None)
        mod, r, m = amc_step(be, target, seed=it)
        led.append(LedgerEntry(
            iteration=it, structure_id=f"{mod}_link",
            edit_description=f"AMC @ SNR≈{es_n0:.0f} dB{' + jammer' if jammer else ''}",
            metrics={"BER": m.ber, "EVM": round(m.evm_pct, 2), "SNR_est": round(m.snr_db, 2)},
            verdict="kept", loop="outer"))
        tw.write(result=r, metrics=m, target_ber=target,
                 structure={"modcod": mod, "center_freq_hz": 2.4e9},
                 active_loop={"loop": "outer",
                              "detail": f"AMC → {mod.upper()} ({BPS[mod]} b/sym){' · jammed' if jammer else ''}"},
                 goal=GOAL, control=c)
        print(f"  [{it:>3}] SNR≈{es_n0:>4.0f}dB {'JAM ' if jammer else '    '}→ {mod:>6}  "
              f"BER={m.ber:.2e}", flush=True)
        time.sleep(max(0.05, args.pause))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
