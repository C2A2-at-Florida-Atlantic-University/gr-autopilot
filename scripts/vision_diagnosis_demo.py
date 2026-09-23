#!/usr/bin/env python3
"""The agent SEES the signal: render scope images and diagnose the fault from the picture.

Two parts, no radios:

  1. a GALLERY of the five faults the vision step tells apart — clean, phase_offset, carrier_unlocked,
     low_snr, interference — rendered to runs/figures/vision/ and diagnosed by the heuristic. A real
     vision-language model reads the same PNGs (the images are the input modality).
  2. VISION IN THE LOOP over the MCP tool surface: a phase-broken 16-QAM link, where a scalar BER only
     says "bad". diagnose_signal SEES a tight-but-rotated grid, calls it a carrier phase offset (not
     low SNR), and the agent tunes the phase (BO) and recovers 16-QAM — the picture drove the fix.

Run:  python scripts/vision_diagnosis_demo.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.perception.diagnose import diagnose_constellation
from gr_autopilot.perception.render import constellation_png, spectrum_png
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer

OUT = Path(__file__).resolve().parents[1] / "runs" / "figures" / "vision"
SR = 2_084_000.0


def _syms(es, phase=0.0, bits=40000):
    r = NumpySimBackend().run_link(LinkParams(modulation="16qam", n_payload_bits=bits,
                                              es_n0_db=es, phase_offset_rad=phase, seed=1))
    return r.rx_syms


def render_gallery() -> int:
    x = _syms(20.0)
    n = np.arange(x.size)
    raw = _syms(9.0)[:16384].astype(complex)
    raw = raw + 6.0 * np.exp(1j * 2 * np.pi * 600e3 * np.arange(raw.size) / SR)
    scen = {
        "clean": (_syms(20.0), None),
        "phase_offset": (_syms(20.0, phase=0.5), None),
        "low_snr": (_syms(6.0), None),
        "carrier_unlocked": (x * np.exp(1j * 2 * np.pi * 8 * n / x.size), None),
        "interference": (_syms(9.0), raw),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"gallery -> {OUT.relative_to(OUT.parents[2])}/  (a real VLM reads these PNGs):")
    ok = True
    for name, (s, rw) in scen.items():
        constellation_png(s, OUT / f"{name}.png", title=name)
        if rw is not None:
            spectrum_png(rw, OUT / f"{name}_spectrum.png", title=f"{name} spectrum")
        d = diagnose_constellation(s, samples=rw, sample_rate=SR)
        ok &= d.fault == name
        print(f"  {name:16} -> {d.fault:16} [{'OK' if d.fault == name else 'MISS'}]  {d.summary}")
    return 0 if ok else 1


def vision_in_the_loop() -> None:
    # a phase-broken 16-QAM channel: high SNR, but a 0.5 rad carrier offset the agent is NOT told about
    workdir = Path(tempfile.mkdtemp(prefix="gr_autopilot_vision_"))
    svc = AutopilotService(channel={"es_n0_db": 18.0, "phase_offset_rad": 0.5},
                           ledger_path=workdir / "s.jsonl")
    cli = InProcessClient(StdioMCPServer(svc))
    print("\nvision in the loop over MCP — a phase-broken 16-QAM link:")
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("16qam").to_dict())
    cli.call("run_flowgraph", n_bits=40000)
    m0 = cli.call("get_metrics")
    print(f"  16-QAM: BER={m0['BER']:.2e}  — scalar metrics only say 'bad'")

    d = cli.call("diagnose_signal", render_path=str(workdir / "before.png"))
    print(f"  diagnose_signal SEES: {d['fault']} (conf {d['confidence']}) — {d['summary']}")
    print(f"    recommended: {d['action']}")

    if d["fault"] == "phase_offset":
        print("  -> the picture says PHASE, not SNR: tune the carrier instead of dropping the rate")
        cli.call("start_bo_run", params=["phase_correction_rad"], bounds=[[-0.7854, 0.7854]],
                 target_ber=1e-2, budget=16)
        cli.call("run_flowgraph", n_bits=40000)
        m1 = cli.call("get_metrics")
        d2 = cli.call("diagnose_signal")
        print(f"  after BO phase tune: BER={m1['BER']:.2e}, diagnose_signal='{d2['fault']}' "
              f"-> 16-QAM kept (the vision call drove the fix)")


def main() -> int:
    rc = render_gallery()
    vision_in_the_loop()
    print("\n=> the agent diagnoses faults from the scope image a bench engineer would read; the "
          "phase-vs-SNR call is one a scalar BER cannot make.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
