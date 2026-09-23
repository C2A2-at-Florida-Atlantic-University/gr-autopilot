"""The jammer as a daemon-owned operator instrument.

No HackRF is keyed here: the interferer is replaced by a fake that records what it was asked to
do. What is under test is the boundary -- that the operator can drive a jammer the agent cannot
see, cannot reach, and is never told about.
"""
import pytest

from gr_autopilot.daemon import OperatorControl
from gr_autopilot.hardware.jammer import JammerError, JammerInstrument


class FakeInterferer:
    """Stands in for HackRFInterferer. ``fail`` reproduces a launch that dies during settle."""

    instances = []

    def __init__(self, center_freq_hz=2.4e9, if_gain=20, kind="cw", follow=False,
                 sample_rate=2_084_000, fail=False):
        self.center_freq_hz = float(center_freq_hz)
        self.if_gain = if_gain
        self.kind = kind
        self.follow = follow
        self.fail = fail
        self.started = self.closed = False
        self.retune_count = 0
        self.last_latency_s = 0.0
        FakeInterferer.instances.append(self)

    def start(self):
        if self.fail:
            raise RuntimeError("hackrf_transfer exited with code 1 during startup")
        self.started = True

    def running(self):
        return self.started and not self.closed

    def close(self):
        self.closed = True
        self.started = False

    def retune(self, f):
        self.center_freq_hz = float(f)
        self.retune_count += 1
        self.last_latency_s = 1.0
        return 1.0


@pytest.fixture(autouse=True)
def _clear():
    FakeInterferer.instances.clear()


def _inst(**kw):
    kw.setdefault("enabled", True)
    kw.setdefault("factory", lambda **c: FakeInterferer(**c))
    return JammerInstrument(**kw)


# --- the interlock ---------------------------------------------------------------------------

def test_a_daemon_without_the_flag_refuses_to_key_anything():
    j = _inst(enabled=False)
    with pytest.raises(JammerError, match="--jammer"):
        j.apply({"jammer": True})
    assert not j.is_armed() and FakeInterferer.instances == []


def test_arm_and_disarm():
    j = _inst()
    st = j.apply({"jammer": True, "jammer_freq_hz": 2.401e9, "jammer_if_gain": 24})
    assert st["armed"] and st["center_freq_hz"] == 2.401e9 and st["if_gain"] == 24
    assert FakeInterferer.instances[-1].started
    st = j.apply({"jammer": False})
    assert not st["armed"] and FakeInterferer.instances[-1].closed


def test_moving_an_armed_jammer_restarts_it_at_the_new_frequency():
    # A CLI-driven HackRF has no live retune; restarting is the only way to move it.
    j = _inst()
    j.apply({"jammer": True, "jammer_freq_hz": 2.400e9})
    first = FakeInterferer.instances[-1]
    j.apply({"jammer_freq_hz": 2.402e9})
    second = FakeInterferer.instances[-1]
    assert first.closed and second is not first
    assert second.center_freq_hz == 2.402e9 and second.started


# --- refusal, not clamping -------------------------------------------------------------------

@pytest.mark.parametrize("cmd,msg", [
    ({"jammer_freq_hz": 12e9}, "outside the HackRF range"),
    ({"jammer_if_gain": 99}, "outside the TX VGA range"),
    ({"jammer_kind": "chirp"}, "unknown"),
])
def test_out_of_scope_commands_are_refused_not_silently_moved(cmd, msg):
    j = _inst()
    with pytest.raises(JammerError, match=msg):
        j.apply({"jammer": True, **cmd})
    assert not j.is_armed()


def test_a_failed_launch_is_reported_and_never_reads_as_armed():
    # The failure this guards: a dead jammer looks exactly like a clean band, which would
    # fabricate a Stage-2 result.
    j = JammerInstrument(enabled=True, factory=lambda **c: FakeInterferer(**c, fail=True))
    with pytest.raises(JammerError, match="did not start"):
        j.apply({"jammer": True})
    st = j.status()
    assert not st["armed"] and "exited with code 1" in st["error"]


# --- following -------------------------------------------------------------------------------

def test_a_following_jammer_chases_the_links_channel():
    link = {"f": 2.400e9}
    j = _inst(link_freq=lambda: link["f"])
    j.apply({"jammer": True, "jammer_follow": True, "jammer_freq_hz": 2.400e9})
    itf = FakeInterferer.instances[-1]
    link["f"] = 2.406e9
    import time
    for _ in range(40):                       # poll interval is 0.25 s
        if itf.retune_count:
            break
        time.sleep(0.05)
    assert itf.center_freq_hz == 2.406e9 and itf.retune_count == 1
    j.apply({"jammer": False})


def test_shutdown_stops_a_running_transmitter():
    j = _inst()
    j.apply({"jammer": True})
    j.shutdown()
    assert FakeInterferer.instances[-1].closed


# --- the integrity boundary ------------------------------------------------------------------

def test_the_agent_tool_surface_never_mentions_the_jammer():
    from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer
    svc = AutopilotService()
    cli = InProcessClient(StdioMCPServer(svc))
    devices = cli.call("list_devices")
    assert not any("hackrf" in d["device_id"].lower() for d in devices)
    assert all(d["device_id"] in ("pluto-a", "pluto-b") for d in devices)
    # No tool exists to arm, move, read or even detect it.
    tools = StdioMCPServer(svc).tools
    assert not any("jam" in t.name.lower() for t in tools)
    assert "jammer" not in str(cli.call("get_status")).lower()


def test_operator_control_drives_the_instrument_and_reports_refusals():
    j = _inst()
    ctl = OperatorControl(j)
    st = ctl.apply({"jammer": True, "jammer_freq_hz": 2.401e9})
    assert st["jammer"] is True and st["jammer_status"]["armed"]
    assert ctl.get()["jammer_status"]["center_freq_hz"] == 2.401e9
    with pytest.raises(JammerError):
        ctl.apply({"jammer_freq_hz": 99e9})
    # A non-jammer command still works and does not disturb the transmitter.
    assert ctl.apply({"target_ber": 1e-3})["target_ber"] == 1e-3
    assert j.is_armed()


# --- the hidden channel condition is operator property too --------------------------------------

class _Svc:
    def __init__(self):
        self.channel = {"tx_atten_db": 40.0}

    def update_channel(self, **changes):
        self.channel.update(changes)
        return dict(self.channel)


def test_operator_can_move_the_hidden_condition_without_dropping_the_rest():
    # A campaign steps one knob at a time; replacing the whole condition would silently drop the
    # others and quietly change the experiment underneath the run.
    svc = _Svc()
    ctl = OperatorControl(_inst(), service=svc)
    st = ctl.apply({"rx_gain_db": 55})
    assert svc.channel == {"tx_atten_db": 40.0, "rx_gain_db": 55.0}
    assert st["channel"]["tx_atten_db"] == 40.0


def test_condition_values_are_clamped_to_what_the_radio_can_do():
    svc = _Svc()
    ctl = OperatorControl(_inst(), service=svc)
    ctl.apply({"tx_atten_db": 500, "rx_gain_db": -99})
    assert svc.channel["tx_atten_db"] == 89.0        # Pluto TX attenuation ceiling
    assert svc.channel["rx_gain_db"] == -3.0         # Pluto RX gain floor


def test_the_agent_has_no_tool_that_touches_the_hidden_condition():
    from gr_autopilot.tools import AutopilotService, StdioMCPServer
    tools = StdioMCPServer(AutopilotService()).tools
    assert not any(("atten" in t.name.lower()) or ("rx_gain" in t.name.lower()) for t in tools)


# --- a follower must see a HOPPING link, not just a retuning one --------------------------------

def test_current_link_freq_follows_a_hop_plan_not_just_a_retune():
    """The Stage-3 gap: observing only center_freq_hz makes a follower blind to hopping.

    Setting a hop plan leaves center_freq_hz untouched, so a follower reading that field parks on
    the last static channel and reports retune_count 0 -- while the experiment claims it chased.
    """
    from gr_autopilot.link.interference_sim import InterferenceSimBackend
    from gr_autopilot.tools import AutopilotService
    svc = AutopilotService(backend=InterferenceSimBackend())

    svc.set_center_freq(2.400e9)
    assert svc.current_link_freq() == 2.400e9

    channels = [2.400e9, 2.402e9, 2.404e9, 2.406e9]
    svc.set_hop_plan(channels_hz=channels, hop_rate_hz=50.0)
    # center_freq_hz has NOT moved -- that is exactly the trap.
    assert svc._agent_rf["center_freq_hz"] == 2.400e9
    # ...but the link is now spread over the hop set, and an observer sees that.
    import time
    seen = set()
    for _ in range(400):
        seen.add(svc.current_link_freq())
        time.sleep(0.002)
    assert seen <= set(channels)
    assert len(seen) > 1, "a follower would see a stationary link and never chase it"

    svc.clear_hop_plan()
    assert svc.current_link_freq() == 2.400e9


def test_link_freq_is_none_when_nothing_is_tuned():
    from gr_autopilot.tools import AutopilotService
    assert AutopilotService().current_link_freq() is None


# ---- wave files must stream continuously through hackrf_transfer -R ---------------------------

def test_wave_files_are_whole_usb_transfers(tmp_path):
    """hackrf_transfer reads the file once per 262,144-byte transfer and pads the remainder after
    a rewind; a short file is a pulse train (16 % duty measured on 2026-09-08). Both writers must
    produce a whole number of transfers, at least eight of them."""
    from gr_autopilot.hardware.interferer import (HACKRF_TRANSFER_SAMPLES, loop_length,
                                                  write_band_noise, write_cw_tone)
    T = 2 * HACKRF_TRANSFER_SAMPLES          # bytes per transfer
    write_cw_tone(str(tmp_path / "cw.c8"), 2_084_000, 100e3)
    write_band_noise(str(tmp_path / "n.c8"), 2_084_000, 1e6)
    for name in ("cw.c8", "n.c8"):
        size = (tmp_path / name).stat().st_size
        assert size >= 8 * T
        assert size % T == 0, f"{name}: {size} bytes is not a whole number of transfers"
    assert loop_length(1) == 8 * HACKRF_TRANSFER_SAMPLES              # floor at ~0.5 s
    assert loop_length(8 * HACKRF_TRANSFER_SAMPLES) == 8 * HACKRF_TRANSFER_SAMPLES
    assert loop_length(8 * HACKRF_TRANSFER_SAMPLES + 1) == 9 * HACKRF_TRANSFER_SAMPLES
    assert loop_length(20_840) == 8 * HACKRF_TRANSFER_SAMPLES         # the old default is rounded up


def test_cw_tone_loops_seamlessly_at_the_requested_offset(tmp_path):
    import numpy as np
    from gr_autopilot.hardware.interferer import write_cw_tone
    write_cw_tone(str(tmp_path / "cw.c8"), 2_084_000, 100e3)
    iq = np.fromfile(tmp_path / "cw.c8", dtype=np.int8).astype(float)
    x = iq[0::2] + 1j * iq[1::2]
    spec = np.abs(np.fft.fft(x)); f = np.fft.fftfreq(len(x), 1 / 2_084_000)
    assert abs(f[np.argmax(spec)] - 100e3) < 5.0                     # snapped within one bin
    # continuity across the loop seam: phase step from last to first sample equals one tone step
    step = np.angle(x[1:] / x[:-1]).mean(); seam = np.angle(x[0] / x[-1])
    assert abs(seam - step) < 0.05
