"""Frequency-avoidance controller in simulation (Stage 2): sensed + blind modes, and the
reactive-jammer escalation. No radios -- the hardware-free proof of the control logic that
scripts/run_hw_interference.py runs unchanged on the Plutos + HackRF.
"""
from gr_autopilot.control.avoidance import FrequencyAvoidanceController
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend

CANDIDATES = [2.400e9, 2.401e9, 2.405e9, 2.410e9, 2.420e9]


def _mhz_map(occ):
    return {round(o["center_freq_hz"] / 1e6): o for o in occ}


def test_backend_jammed_on_channel_clean_off_channel():
    itf = Interferer(center_freq_hz=2.4e9, inr_db=30.0, kind="cw")
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=itf, center_freq_hz=2.4e9)
    assert be.effective_es_n0_db() < 0.0            # buried on-channel
    be.set_condition(center_freq_hz=2.401e9)        # 1 MHz away
    assert be.effective_es_n0_db() == 20.0          # fully clean


def test_monitor_senses_the_jammer():
    itf = Interferer(center_freq_hz=2.400e9, inr_db=30.0, kind="cw")
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=itf, center_freq_hz=2.400e9)
    occ = _mhz_map(be.sense_spectrum(CANDIDATES))
    assert occ[2400]["occupied"] and occ[2400]["power_db"] > 25    # jammer peak
    assert not occ[2401]["occupied"] and occ[2401]["power_db"] < 1  # clear 1 MHz away


def test_sensing_beats_always_on_jammer():
    """Monitor role: sense the band, retune straight to a clean channel, restore with AMC."""
    itf = Interferer(center_freq_hz=2.400e9, inr_db=30.0, kind="cw")
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=itf, center_freq_hz=2.400e9)
    res = FrequencyAvoidanceController(be, CANDIDATES, target_ber=1e-2,
                                       probe_bits=8000, confirm_bits=8000).run()
    assert res.mode == "sensed"
    occ = _mhz_map(res.occupancy)
    assert occ[2400]["occupied"] and not occ[2401]["occupied"]
    assert res.chosen_freq_hz is not None and res.chosen_freq_hz != 2.400e9  # moved off jammer
    assert res.beaten is True and res.amc.chosen in ("qpsk", "16qam")        # link restored


def test_blind_mode_also_avoids():
    itf = Interferer(center_freq_hz=2.400e9, inr_db=30.0, kind="cw")
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=itf, center_freq_hz=2.400e9)
    res = FrequencyAvoidanceController(be, CANDIDATES, target_ber=1e-2, probe_bits=8000,
                                       confirm_bits=8000, use_monitor=False).run()
    assert res.mode == "blind"
    assert res.scan[0]["clean"] is False               # probed current channel, jammed
    assert res.chosen_freq_hz is not None and res.beaten is True


def test_reactive_jammer_defeats_sensing():
    """A reactive jammer is idle while the link is silent (energy detection sees the band clear),
    then follows the link when it transmits -- so a sensed-clean channel still fails."""
    itf = Interferer(center_freq_hz=2.400e9, inr_db=30.0, kind="cw", reactive=True)
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=itf, center_freq_hz=2.400e9)
    res = FrequencyAvoidanceController(be, CANDIDATES, target_ber=1e-2,
                                       probe_bits=8000, confirm_bits=8000).run()
    assert res.mode == "sensed"
    assert all(not o["occupied"] for o in res.occupancy)   # looks clear everywhere
    assert res.chosen_freq_hz is not None                  # so it retunes there...
    assert res.beaten is False                             # ...but the link is still jammed

    # blind mode fails too: every probe transmits, so the jammer fires on every candidate
    resb = FrequencyAvoidanceController(be, CANDIDATES, target_ber=1e-2, probe_bits=8000,
                                        confirm_bits=8000, use_monitor=False).run()
    assert resb.chosen_freq_hz is None


def test_partial_band_noise_occupies_a_range():
    """A band-limited noise jammer spans several channels; the monitor sees the extent and the
    agent retunes *beyond* it (mirrors the 2 MHz-noise hardware run)."""
    itf = Interferer(center_freq_hz=2.400e9, inr_db=40.0, kind="noise", bandwidth_hz=2e6)
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=itf, center_freq_hz=2.400e9)
    res = FrequencyAvoidanceController(be, CANDIDATES, target_ber=1e-2,
                                       probe_bits=8000, confirm_bits=8000).run()
    occ = _mhz_map(res.occupancy)
    assert occ[2400]["occupied"] and occ[2401]["occupied"]   # jammer spans two channels
    assert not occ[2405]["occupied"]                          # clear beyond it
    assert res.chosen_freq_hz is not None and res.chosen_freq_hz >= 2.405e9
    assert res.beaten is True


def test_no_clean_channel_when_interferer_is_wideband():
    # High-power band-limited noise across all candidates -> every channel senses occupied.
    itf = Interferer(center_freq_hz=2.410e9, inr_db=50.0, kind="noise", bandwidth_hz=30e6)
    be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=itf, center_freq_hz=2.400e9)
    res = FrequencyAvoidanceController(be, CANDIDATES, target_ber=1e-2,
                                       probe_bits=8000, confirm_bits=8000).run()
    assert all(o["occupied"] for o in res.occupancy)
    assert res.chosen_freq_hz is None and res.amc is None
