"""The LLM-vs-two-loop ablation harness: ground truth tracks the AMC boundary, both arms reach it,
and the measurement-guided (LLM) policy converges at far fewer link runs — the sample-efficiency
claim. Fast settings (40k bits), no hardware, no `mcp` SDK."""
from gr_autopilot.link.backend import LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from scripts.llm_ablation import CountingBackend, ground_truth, run_guided, run_two_loop, sweep

BITS = 40_000
# Detecting a *phase* rotation from the BER-vs-EVM gap needs fine BER resolution (a coarse
# measurement floor can't tell BER=0 from a boundary SNR), so the phase cases use more bits.
PHASE_BITS = 200_000


def test_ground_truth_tracks_the_amc_boundary():
    assert ground_truth(6, 1e-2, BITS) == "bpsk"
    assert ground_truth(12, 1e-2, BITS) == "qpsk"
    assert ground_truth(18, 1e-2, BITS) == "16qam"


def test_counting_backend_tallies_runs():
    cb = CountingBackend(NumpySimBackend())
    for _ in range(3):
        cb.run_link(LinkParams(modulation="qpsk", n_payload_bits=1000, es_n0_db=12.0))
    assert cb.runs == 3


def test_two_loop_cost_is_exhaustive():
    # deterministic cost model: 3 rungs x (bo_budget trials + 1 confirm), regardless of SNR
    chosen, runs = run_two_loop(18, 1e-2, BITS, bo_budget=6, seed=0)
    assert chosen == "16qam"
    assert runs == 3 * (6 + 1)


def test_guided_matches_ground_truth_but_cheaper():
    for snr in (6, 12, 18):
        truth = ground_truth(snr, 1e-2, BITS)
        g_choice, g_runs = run_guided(snr, 0.0, 1e-2, BITS, bo_budget=6, seed=0)
        tl_choice, tl_runs = run_two_loop(snr, 1e-2, BITS, bo_budget=6, seed=0)
        assert g_choice == truth == tl_choice   # same, correct decision
        assert g_runs < tl_runs                 # but the guided policy is cheaper
        assert g_runs <= 3                       # clean sweep needs no BO


def test_guided_diagnoses_phase_and_rescues_16qam():
    # high SNR but a phase rotation breaks 16qam -> BO rescue -> keep 16qam (the two loops cooperate)
    choice, runs = run_guided(18.0, 0.41, 1e-2, PHASE_BITS, bo_budget=12, seed=0)
    assert choice == "16qam"
    assert runs > 3   # it spent the inner loop rescuing the rung


def test_guided_does_not_falsely_rescue_snr_limited_16qam():
    # mid SNR + phase: 16qam is unreachable even corrected -> keep qpsk, don't waste BO on it
    choice, runs = run_guided(12.0, 0.35, 1e-2, PHASE_BITS, bo_budget=12, seed=0)
    assert choice == "qpsk"
    assert runs <= 3


def test_sweep_both_arms_hit_the_boundary_guided_cheaper():
    rows = sweep([6, 12, 18], 1e-2, BITS, bo_budget=6, seed=0)
    assert all(r.two_loop == r.truth == r.guided for r in rows)          # 100% accuracy, both arms
    assert sum(r.guided_runs for r in rows) < sum(r.two_loop_runs for r in rows)
