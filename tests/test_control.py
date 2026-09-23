"""Two-loop control tests: AMC ground truth + reference-controller rediscovery (in sim)."""
import math

import pytest

from gr_autopilot.control import TwoLoopController, amc_sweep
from gr_autopilot.link.numpy_sim import NumpySimBackend

# SNR settings chosen so the optimal modcod is unambiguous at BER target 1e-3:
#   8 dB -> BPSK only; 12 dB -> up to QPSK; 18 dB -> up to 16-QAM.
LOW, MID, HIGH = 8.0, 12.0, 18.0


def test_amc_ground_truth_is_a_staircase():
    cells = amc_sweep(NumpySimBackend(), [LOW, MID, HIGH], target_ber=1e-3, n_bits=300_000)
    opt = {c.snr_db: c.optimal for c in cells}
    assert opt[LOW] == "bpsk"
    assert opt[MID] == "qpsk"
    assert opt[HIGH] == "16qam"


@pytest.mark.parametrize("snr,expected", [(LOW, "bpsk"), (MID, "qpsk"), (HIGH, "16qam")])
def test_controller_rediscovers_optimal_modcod(snr, expected):
    # The controller is given only the channel (never the SNR value) and must converge to
    # the theoretically optimal modcod from measurements alone.
    ctrl = TwoLoopController(NumpySimBackend(), target_ber=1e-3, bo_budget=8,
                             trial_bits=40_000, confirm_bits=300_000, seed=0)
    res = ctrl.run({"es_n0_db": snr})
    assert res.chosen == expected


def test_inner_loop_corrects_phase_offset():
    # A residual carrier phase would wreck 16-QAM if uncorrected; the BO inner loop must
    # discover the de-rotation so the high-SNR link still reaches 16-QAM.
    offset = 0.5  # radians (hidden channel)
    ctrl = TwoLoopController(NumpySimBackend(), target_ber=1e-3, bo_budget=20,
                            trial_bits=40_000, confirm_bits=300_000, seed=1)
    res = ctrl.run({"es_n0_db": HIGH, "phase_offset_rad": offset})
    assert res.chosen == "16qam"
    # The tuned correction should be close to the true offset.
    assert res.best_knobs["16qam"]["phase_correction_rad"] == pytest.approx(offset, abs=0.1)


def test_uncorrected_phase_offset_breaks_16qam():
    # Sanity: with no correction the same offset makes 16-QAM fail (so the fix above is real).
    from gr_autopilot.flowgraph import FlowgraphSpec, build_and_run
    from gr_autopilot.scoring.metrics import compute_metrics
    spec = FlowgraphSpec.link("16qam")
    r = build_and_run(spec, NumpySimBackend(), n_payload_bits=100_000,
                      es_n0_db=HIGH, phase_offset_rad=0.5, seed=1)
    m = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
    assert m.ber > 1e-3


def test_spectral_efficiency_objective():
    # Inner-loop knob for spectral efficiency: tighter pulse (lower rolloff) and higher-order
    # modulation both raise bits/s/Hz; the bandwidth loss ranks any BER-failing config strictly
    # worse than every feasible one, so the BO drives to the tightest pulse that still decodes.
    from types import SimpleNamespace

    from gr_autopilot.control.objective import bandwidth_loss, bits_per_hz
    assert bits_per_hz("qpsk", 0.2) > bits_per_hz("qpsk", 0.5)
    assert bits_per_hz("16qam", 0.35) > bits_per_hz("qpsk", 0.35) > bits_per_hz("bpsk", 0.35)
    ok, bad = SimpleNamespace(ber=1e-4), SimpleNamespace(ber=0.4)
    assert bandwidth_loss(ok, 0.3, 1e-2) == pytest.approx(1.3)          # feasible: 1 + rolloff
    assert bandwidth_loss(bad, 0.25, 1e-2) > bandwidth_loss(ok, 0.6, 1e-2)  # any fail > any pass


def test_coded_modcod_ladder():
    # A ladder can carry coded modcods ("mod:coding"); spectral efficiency accounts for rate.
    from gr_autopilot.control.objective import parse_modcod, spectral_efficiency
    assert parse_modcod("16qam:conv_k3_r12") == ("16qam", "conv_k3_r12")
    assert spectral_efficiency("qpsk") == 2.0
    assert spectral_efficiency("qpsk:conv_k3_r12") == 1.0            # 2 bits x rate 1/2

    # The controller runs a mixed uncoded/coded ladder; at high SNR the highest-rate uncoded
    # modcod wins (coding only wastes throughput), and the coded link is evaluated and decodes.
    ctrl = TwoLoopController(NumpySimBackend(), target_ber=1e-3, knobs={},
                             ladder=("bpsk", "qpsk", "16qam", "qpsk:conv_k3_r12"),
                             trial_bits=40_000, confirm_bits=40_000, seed=0)
    res = ctrl.run({"es_n0_db": 18.0})
    assert res.chosen == "16qam"
    assert res.per_mod["qpsk:conv_k3_r12"].ber == 0.0


def test_coded_amc_win_with_rate34():
    # The coded-AMC WIN: at a marginal SNR uncoded QPSK fails but rate-3/4 coded QPSK meets the
    # target, so the agent ADDS CODING to hold 1.5 bits/s/Hz -- higher than the BPSK fallback (1).
    ctrl = TwoLoopController(NumpySimBackend(), target_ber=1e-3, knobs={},
                             ladder=("bpsk", "qpsk", "qpsk:conv_k3_r34", "16qam"),
                             trial_bits=40_000, confirm_bits=40_000, seed=0)
    res = ctrl.run({"es_n0_db": 9.0})
    assert res.chosen == "qpsk:conv_k3_r34"
    assert res.per_mod["qpsk"].ber > 1e-3                  # uncoded QPSK fails here
    assert res.per_mod["qpsk:conv_k3_r34"].ber <= 1e-3     # coded QPSK meets the target


def test_coding_gain_in_controller():
    # At a marginal SNR the coded link has a lower info-BER than the same modulation uncoded.
    ctrl = TwoLoopController(NumpySimBackend(), target_ber=1e-3, knobs={},
                             ladder=("qpsk", "qpsk:conv_k3_r12"),
                             trial_bits=40_000, confirm_bits=40_000, seed=0)
    res = ctrl.run({"es_n0_db": 10.0})
    assert res.per_mod["qpsk:conv_k3_r12"].ber < res.per_mod["qpsk"].ber


def test_ledger_records_trajectory(tmp_path):
    from gr_autopilot.ledger import EditLedger
    led = EditLedger(tmp_path / "run.jsonl")
    ctrl = TwoLoopController(NumpySimBackend(), bo_budget=6, trial_bits=20_000,
                            confirm_bits=100_000, ledger=led, seed=0)
    ctrl.run({"es_n0_db": MID})
    rows = led.read()
    assert len(rows) == 4  # 3 rungs + 1 selection
    assert rows[-1]["verdict"] == "kept"


def test_empty_knobs_disables_the_inner_loop():
    # Regression: an empty dict is falsy, so `dict(knobs or DEFAULT_KNOBS)` used to silently
    # re-enable BO when a caller asked for none (a footgun that ran the inner loop on hardware
    # under an "outer-loop only" comment). None -> default inner loop; {} -> no inner loop.
    from gr_autopilot.control.controller import DEFAULT_KNOBS
    assert TwoLoopController(NumpySimBackend()).knobs == DEFAULT_KNOBS
    assert TwoLoopController(NumpySimBackend(), knobs={}).knobs == {}
