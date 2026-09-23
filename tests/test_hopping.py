"""Frequency hopping (Stage 3): the countermeasure to a reactive jammer avoidance can't beat.
Physics (jammed fraction vs hop rate) + the controller that escalates the hop rate to evade, with
the honest limit (a fast enough jammer out-reacts hopping). Sim only, no radios."""
import pytest

from gr_autopilot.control.avoidance import FrequencyAvoidanceController
from gr_autopilot.control.hopping import FrequencyHoppingController
from gr_autopilot.link.hopping import (HopPlan, hopped_effective_es_n0_db, jammed_fraction,
                                       min_evading_hop_rate_hz)
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.link.numpy_sim import NumpySimBackend

CHANS = tuple(2.400e9 + k * 1e6 for k in range(8))
BW = 350_000.0


def test_reactive_jammed_fraction_falls_to_zero_at_one_over_tau():
    jam = Interferer(center_freq_hz=2.4e9, reactive=True, reaction_s=5e-3)   # need > 200 Hz
    assert min_evading_hop_rate_hz(jam) == 200.0
    assert jammed_fraction(jam, HopPlan(CHANS, 50), BW) == 0.75              # 1 - 5e-3*50
    assert jammed_fraction(jam, HopPlan(CHANS, 100), BW) == 0.5
    assert jammed_fraction(jam, HopPlan(CHANS, 200), BW) == 0.0             # out-hopped
    assert jammed_fraction(jam, HopPlan(CHANS, 400), BW) == 0.0
    # above 1/tau the effective SNR is the clean SNR (no in-band interference on average)
    assert hopped_effective_es_n0_db(jam, HopPlan(CHANS, 400), BW, 20.0) == 20.0


def test_fixed_jammer_hopping_spreads_the_risk():
    jam = Interferer(center_freq_hz=CHANS[0], inr_db=30.0, kind="cw", reactive=False)
    assert jammed_fraction(jam, HopPlan(CHANS, 100), BW) == 1 / len(CHANS)  # 1 of N channels hit
    assert min_evading_hop_rate_hz(jam) is None                            # not a reaction game


def test_hopping_evades_a_slow_reactive_jammer():
    jam = Interferer(center_freq_hz=2.4e9, inr_db=30.0, reactive=True, reaction_s=5e-3)  # > 200 Hz
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=jam, center_freq_hz=2.4e9)
    # Stage 2: avoidance cannot beat a reactive jammer (sensed-clear but still jammed)
    av = FrequencyAvoidanceController(be, CHANS[:5], target_ber=1e-2,
                                      probe_bits=8000, confirm_bits=8000).run()
    assert av.beaten is False
    be.set_condition(hop_plan=None, center_freq_hz=2.4e9)                   # reset before hopping
    res = FrequencyHoppingController(be, CHANS, (50, 100, 200, 400), target_ber=1e-2).run()
    assert res.evaded is True and res.hop_rate_hz == 200.0                  # min evading rate found
    assert res.beaten is True and res.amc.chosen in ("qpsk", "16qam")       # link restored + AMC
    assert [t["evaded"] for t in res.trajectory] == [False, False, True]    # the escalation knee


def test_hopped_sim_grades_per_dwell_not_at_the_averaged_snr():
    # regression guard for the Jensen bug: in the transition region the hopped link must grade as a
    # MIXTURE of clean + fully-jammed dwells (matching the hardware per-dwell path), NOT as one link
    # at the time-averaged SNR. The two disagree because BER is nonlinear in noise power.
    from gr_autopilot.link.backend import LinkParams
    from gr_autopilot.scoring.metrics import compute_metrics
    jam = Interferer(center_freq_hz=2.4e9, inr_db=25.0, reactive=True, reaction_s=5e-3)
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=jam, center_freq_hz=2.4e9)
    hp = HopPlan(CHANS, 100)                                    # f = 1 - 5e-3*100 = 0.5 jammed
    f = jammed_fraction(jam, hp, BW)
    assert f == 0.5

    be.set_condition(hop_plan=hp)
    def ber_of(res):
        return compute_metrics(res.tx_bits, res.rx_bits, res.rx_syms, res.ref_syms).ber
    hopped = ber_of(be.run_link(LinkParams(modulation="qpsk", n_payload_bits=60_000, sps=8, seed=1)))

    # the per-dwell mixture: f of the bits fully jammed, (1-f) clean
    clean_ber = ber_of(NumpySimBackend().run_link(LinkParams(modulation="qpsk",
                        n_payload_bits=60_000, es_n0_db=20.0, sps=8, seed=1)))
    snr_jammed = jam.effective_es_n0_db(2.4e9, BW, 20.0)
    jammed_ber = ber_of(NumpySimBackend().run_link(LinkParams(modulation="qpsk",
                        n_payload_bits=60_000, es_n0_db=snr_jammed, sps=8, seed=1)))
    mixture = f * jammed_ber + (1 - f) * clean_ber

    # BER at the (Jensen) averaged SNR — the WRONG value the old code produced
    jensen_ber = ber_of(NumpySimBackend().run_link(LinkParams(modulation="qpsk",
                        n_payload_bits=60_000, es_n0_db=hopped_effective_es_n0_db(jam, hp, BW, 20.0),
                        sps=8, seed=1)))
    assert hopped == pytest.approx(mixture, rel=0.15)          # grades as the per-dwell mixture
    assert abs(hopped - mixture) < abs(hopped - jensen_ber)    # and NOT as the averaged-SNR link


def test_a_fast_reactive_jammer_out_reacts_hopping():
    # honest limit: an FPGA-fast jammer (0.2 ms -> needs > 5 kHz) beats every achievable hop rate
    jam = Interferer(center_freq_hz=2.4e9, inr_db=30.0, reactive=True, reaction_s=0.2e-3)
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=jam, center_freq_hz=2.4e9)
    res = FrequencyHoppingController(be, CHANS, (100, 400, 800, 1600), target_ber=1e-2).run()
    assert res.evaded is False and res.hop_rate_hz is None
    assert all(not t["evaded"] for t in res.trajectory)
