"""Perception: render the scope image + diagnose the fault from the constellation (no radios).

Checks the five faults the vision step tells apart, the renderer, and the diagnose_signal MCP tool
(including that it derives everything from the received signal and never leaks the hidden SNR).
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.perception.diagnose import HeuristicVision, diagnose_constellation
from gr_autopilot.perception.render import constellation_png, spectrum_png
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer

SR = 2_084_000.0


def _syms(es, phase=0.0, bits=40000):
    return NumpySimBackend().run_link(LinkParams(modulation="16qam", n_payload_bits=bits,
                                                 es_n0_db=es, phase_offset_rad=phase, seed=1)).rx_syms


def _scenarios():
    x = _syms(20.0)
    n = np.arange(x.size)
    raw = _syms(9.0)[:16384].astype(complex)
    raw = raw + 6.0 * np.exp(1j * 2 * np.pi * 600e3 * np.arange(raw.size) / SR)
    return {
        "clean": (_syms(20.0), None),
        "phase_offset": (_syms(20.0, phase=0.5), None),
        "low_snr": (_syms(6.0), None),
        "carrier_unlocked": (x * np.exp(1j * 2 * np.pi * 8 * n / x.size), None),
        "interference": (_syms(9.0), raw),
    }


@pytest.mark.parametrize("name", ["clean", "phase_offset", "low_snr", "carrier_unlocked", "interference"])
def test_diagnoses_each_fault(name):
    syms, raw = _scenarios()[name]
    d = diagnose_constellation(syms, samples=raw, sample_rate=SR)
    assert d.fault == name
    assert d.action and d.summary                        # actionable + human-readable
    assert 0.0 <= d.confidence <= 1.0


def test_clean_vs_phase_offset_is_a_rotation_not_snr():
    # both are tight (good SNR); the picture distinguishes them by rotation, which BER cannot
    clean = diagnose_constellation(_syms(20.0))
    rotated = diagnose_constellation(_syms(20.0, phase=0.5))
    assert clean.fault == "clean" and rotated.fault == "phase_offset"
    assert abs(clean.features["rotation_deg"]) < 8.0
    assert abs(rotated.features["rotation_deg"]) > 20.0
    assert rotated.features["derot_evm"] < 0.2           # still a tight grid, just turned


def test_low_snr_is_not_mistaken_for_a_phase_offset():
    # BO phase tuning cannot rescue noise; the diagnosis must not send the agent chasing phase
    assert diagnose_constellation(_syms(6.0)).fault == "low_snr"


def _clean_syms(mod, es=22.0):
    return NumpySimBackend().run_link(LinkParams(modulation=mod, n_payload_bits=40000,
                                                 es_n0_db=es, seed=1)).rx_syms


@pytest.mark.parametrize("mod,es", [("bpsk", 22.0), ("qpsk", 22.0), ("8psk", 22.0), ("16qam", 22.0),
                                    ("32qam", 26.0), ("64qam", 30.0), ("256qam", 38.0)])
def test_clean_link_diagnosed_clean_for_each_modulation(mod, es):
    # a healthy link at every rung must read 'clean' (climb), not low_snr — scored against ITS grid
    assert diagnose_constellation(_clean_syms(mod, es), modulation=mod).fault == "clean"


def test_bpsk_at_the_benchs_evm_is_clean_not_low_snr():
    # Regression for the false positive seen on the radios: BPSK at ~14-16% EVM (Es/N0 ~16-17 dB),
    # zero bit errors, was called 'low_snr' because the tightness threshold was the 16-QAM number.
    # Against BPSK's own decision distance (2.0) that cloud is tight.
    d = diagnose_constellation(_clean_syms("bpsk", es=16.0), modulation="bpsk")
    assert d.fault == "clean", d.summary
    assert d.features["evm_clean_threshold"] > 0.3


def test_the_same_evm_is_low_snr_for_64qam():
    # ...and the same absolute spread IS a failing 64-QAM link.
    d = diagnose_constellation(_clean_syms("64qam", es=16.0), modulation="64qam")
    assert d.fault == "low_snr"


def test_clean_qpsk_needs_the_modulation_hint():
    # regression guard for the hardcoded-16-QAM-grid bug: clean QPSK scored against the 16-QAM grid
    # is mislabeled low_snr (would tell the agent to DROP the rate it should climb); the modulation
    # hint fixes it.
    q = _clean_syms("qpsk")
    assert diagnose_constellation(q, modulation="16qam").fault == "low_snr"   # the bug
    assert diagnose_constellation(q, modulation="qpsk").fault == "clean"      # fixed


def test_renderers_write_valid_pngs(tmp_path):
    cpath = constellation_png(_syms(20.0), tmp_path / "c.png", title="t")
    spath = spectrum_png(_syms(20.0), tmp_path / "s.png")
    for p in (cpath, spath):
        with open(p, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"    # PNG magic
        assert (tmp_path / p.split("/")[-1]).stat().st_size > 1000


def test_thresholds_are_tunable():
    # a stricter EVM gate reclassifies a marginal grid; the diagnoser is not hard-coded
    marginal = _syms(12.0)
    lenient = HeuristicVision(evm_clean=0.5).diagnose(marginal).fault
    strict = HeuristicVision(evm_clean=0.02).diagnose(marginal).fault
    assert lenient in {"clean", "phase_offset"} and strict == "low_snr"


# -- the diagnose_signal MCP tool --------------------------------------------------------------

def test_diagnose_signal_over_mcp_sees_phase_offset(tmp_path):
    svc = AutopilotService(channel={"es_n0_db": 18.0, "phase_offset_rad": 0.5})
    cli = InProcessClient(StdioMCPServer(svc))
    assert "diagnose_signal" in {t["name"] for t in cli.list_tools()}
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("16qam").to_dict())
    cli.call("run_flowgraph", n_bits=40000)
    d = cli.call("diagnose_signal", render_path=str(tmp_path / "before.png"))
    assert d["fault"] == "phase_offset"                  # the picture, over the wire
    assert "image_path" in d                             # rendered for a VLM client to read


def test_diagnose_signal_needs_a_run_and_never_leaks_snr():
    svc = AutopilotService(channel={"es_n0_db": 18.0, "phase_offset_rad": 0.5})
    cli = InProcessClient(StdioMCPServer(svc))
    with pytest.raises(Exception):
        cli.call("diagnose_signal")                      # no run yet
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("16qam").to_dict())
    cli.call("run_flowgraph", n_bits=20000)
    blob = json.dumps(cli.call("diagnose_signal"))
    for forbidden in ("es_n0", "clean_es_n0", "18.0"):   # the hidden channel must not appear
        assert forbidden not in blob
