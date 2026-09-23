"""Hardware frequency-hopping probe (Stage 3), tested around the bench's real limitation.

No radios: a MockHoppingBackend wraps the numpy AWGN backend and drops to a low Es/N0 whenever the
link lands on a jammer-occupied channel. That exercises the exact aggregation the live probe runs on
the Plutos (control/hw_hopping.HardwareHopProbe), so the physics is checked here and only the wiring
is left for the bench.

The limitation these tests are built around (and that the paper discloses): the link out-hops a
channel-following jammer ONLY while its per-hop dwell is shorter than the jammer's reaction latency.
A follower that keeps up within the dwell (dwells_behind == 0) jams every hop — the honest floor.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from gr_autopilot.control.hw_hopping import HardwareHopProbe, follower_channel
from gr_autopilot.link.backend import LinkBackend, LinkParams
from gr_autopilot.link.hopping import HopPlan, jammed_fraction
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.numpy_sim import NumpySimBackend

CHANS = tuple(2.400e9 + k * 1e6 for k in range(4))  # 2400..2403 MHz, 1 MHz apart


class MockHoppingBackend(LinkBackend):
    """Stand-in for the Pluto link: clean Es/N0 off the jammer, a jammed Es/N0 when the link's
    current channel is in ``jammed_channels`` (which a follower mutates between hops)."""

    name = "mock-hop"
    owns_channel = True

    def __init__(self, clean_es_n0_db: float = 20.0, jammed_es_n0_db: float = -4.0):
        self._sim = NumpySimBackend()
        self.clean = clean_es_n0_db
        self.jammed = jammed_es_n0_db
        self.jammed_channels: set[float] = set()
        self.center_freq_hz = 0.0
        self.settle_s = 0.0

    def set_condition(self, **cond) -> None:
        if cond.get("center_freq_hz") is not None:
            self.center_freq_hz = float(cond["center_freq_hz"])

    def run_link(self, params: LinkParams):
        es = self.jammed if self.center_freq_hz in self.jammed_channels else self.clean
        return self._sim.run_link(replace(params, es_n0_db=es))


def _probe(be, hop_rate=4.0, **kw):
    return HardwareHopProbe(be, HopPlan(CHANS, hop_rate), target_ber=1e-2,
                            probe_bits=4000, sps=8, rolloff=0.35, **kw)


# -- follower_channel physics ------------------------------------------------------------------

def test_follower_channel_keeps_up_jams_current():
    # dwells_behind == 0: a follower fast enough to retune inside the dwell sits on the link's channel
    assert follower_channel([CHANS[0], CHANS[1]], CHANS[2], 0) == CHANS[2]


def test_follower_channel_one_behind_is_previous():
    assert follower_channel([CHANS[0], CHANS[1]], CHANS[2], 1) == CHANS[1]


def test_follower_channel_two_behind():
    assert follower_channel([CHANS[0], CHANS[1], CHANS[2]], CHANS[3], 2) == CHANS[1]


def test_follower_channel_none_before_it_can_chase():
    # nothing visited yet -> the follower has no target on the very first hop
    assert follower_channel([], CHANS[0], 1) is None


# -- fixed jammer: hopping spreads the hits to 1/N ---------------------------------------------

def test_fixed_jammer_spreads_to_one_over_n():
    be = MockHoppingBackend()
    be.jammed_channels = {CHANS[0]}                 # a fixed spot jammer on one of four channels
    res = _probe(be).run(cycles=2)                  # 8 hops, 2 landings on the jammed channel
    assert res.jammed_fraction == pytest.approx(2 / 8)      # == 1/N
    # every non-jammed hop is clean; only the two landings on CHANS[0] are hit
    jammed_idx = [r.hop_index for r in res.records if r.jammed]
    assert jammed_idx == [0, 4]


def test_fixed_jammer_empirical_matches_sim_model():
    # the empirical 1/N must equal link/hopping.jammed_fraction for the same fixed jammer
    be = MockHoppingBackend()
    be.jammed_channels = {CHANS[0]}
    res = _probe(be).run(cycles=3)
    jam = Interferer(center_freq_hz=CHANS[0], kind="cw")
    model = jammed_fraction(jam, HopPlan(CHANS, 4.0), link_bw_hz=400e3)
    assert res.jammed_fraction == pytest.approx(model) == pytest.approx(1 / 4)


def test_camping_on_jammer_is_fully_jammed():
    # the baseline hopping beats: a one-channel "hop set" sitting on the jammer is 100% jammed
    be = MockHoppingBackend()
    be.jammed_channels = {CHANS[0]}
    res = HardwareHopProbe(be, HopPlan((CHANS[0],), 4.0), target_ber=1e-2, probe_bits=4000).run(cycles=4)
    assert res.jammed_fraction == 1.0
    assert not res.evaded


# -- channel-following jammer: evaded while slower than the hop, floor when it keeps up ---------

def _follower_before_hop(be, dwells_behind):
    """Place the mock's jammer where a follower ``dwells_behind`` dwells late would be."""
    def before(t, ch, history):
        tgt = follower_channel(history, ch, dwells_behind)
        be.jammed_channels = {tgt} if tgt is not None else set()
    return before


def test_follower_one_behind_is_evaded():
    be = MockHoppingBackend()
    res = _probe(be).run(cycles=3, before_hop=_follower_before_hop(be, dwells_behind=1))
    # always a step behind (consecutive channels differ) -> never catches the link
    assert res.jammed_fraction == 0.0
    assert res.evaded


def test_follower_that_keeps_up_jams_every_hop_the_floor():
    be = MockHoppingBackend()
    res = _probe(be).run(cycles=3, before_hop=_follower_before_hop(be, dwells_behind=0))
    # reaction shorter than the dwell -> the honest limit: hopping cannot evade it
    assert res.jammed_fraction == 1.0
    assert not res.evaded


@pytest.mark.parametrize("dwells_behind,expect_jf", [(0, 1.0), (1, 0.0), (2, 0.0), (3, 0.0)])
def test_knee_is_at_dwell_equals_reaction(dwells_behind, expect_jf):
    # dwells_behind = round(tau / dwell): the knee is at tau == dwell (== sim's 1/tau hop rate).
    be = MockHoppingBackend()
    res = _probe(be).run(cycles=3, before_hop=_follower_before_hop(be, dwells_behind))
    assert res.jammed_fraction == pytest.approx(expect_jf)


def test_settle_s_threaded_to_backend():
    # the probe sets the per-hop dwell on a backend that exposes settle_s (the Pluto does)
    be = MockHoppingBackend()
    _probe(be, settle_s=0.1).run(cycles=1)
    assert be.settle_s == pytest.approx(0.1)


# -- HoppingBackend: make a hop plan physically cycle channels (the hardware path) --------------

from gr_autopilot.link.backend import LinkParams as _LP  # noqa: E402
from gr_autopilot.link.hopping_backend import HoppingBackend  # noqa: E402


def _ber(res):
    from gr_autopilot.scoring.metrics import compute_metrics
    return compute_metrics(res.tx_bits, res.rx_bits, res.rx_syms, res.ref_syms).ber


def test_hopping_backend_passthrough_without_plan():
    inner = MockHoppingBackend()
    be = HoppingBackend(inner)
    inner.jammed_channels = {CHANS[0]}
    inner.set_condition(center_freq_hz=CHANS[0])
    res = be.run_link(_LP(modulation="qpsk", n_payload_bits=8000, sps=8))
    assert be.center_freq_hz == CHANS[0] and _ber(res) > 1e-2   # transparent: sits on the jammed chan


def test_hopping_backend_cycles_and_beats_camping_on_a_fixed_jammer():
    inner = MockHoppingBackend()
    inner.jammed_channels = {CHANS[0]}                          # fixed jammer on one of four
    camp = HoppingBackend(inner)
    inner.set_condition(center_freq_hz=CHANS[0])
    camped = _ber(camp.run_link(_LP(modulation="qpsk", n_payload_bits=8000, sps=8)))
    be = HoppingBackend(inner)
    be.set_condition(hop_plan=HopPlan(CHANS, 4.0))
    hopped = _ber(be.run_link(_LP(modulation="qpsk", n_payload_bits=8000, sps=8)))
    assert hopped < camped                                      # spreading the hits helps (~1/N)
    assert be.run_link(_LP(modulation="qpsk", n_payload_bits=8000, sps=8)).meta["n_dwells"] == 4


def test_hopping_backend_on_dwell_follower_is_evaded():
    inner = MockHoppingBackend()
    be = HoppingBackend(inner)
    state = {"prev": None}

    def follower(channel):                                       # turn-based, one dwell behind
        inner.jammed_channels = {state["prev"]} if state["prev"] is not None else set()
        state["prev"] = channel

    be.on_dwell = follower
    be.set_condition(hop_plan=HopPlan(CHANS, 200.0))
    res = be.run_link(_LP(modulation="qpsk", n_payload_bits=8000, sps=8))
    assert _ber(res) <= 1e-2                                     # link's channel always clean -> evaded


def test_hopping_backend_delegates_sense_and_condition():
    inner = MockHoppingBackend()
    be = HoppingBackend(inner)
    be.set_condition(center_freq_hz=CHANS[2])                    # non-hop keys pass through
    assert inner.center_freq_hz == CHANS[2]
    be.set_condition(hop_plan=HopPlan(CHANS, 4.0))              # hop key is intercepted
    assert be.hop_plan is not None and inner.center_freq_hz == CHANS[2]


# -- the silent no-op that made a whole Stage-3 run meaningless --------------------------------
#
# A hop plan set on the worker-backed radios never reached a radio: HopPlan was stringified by the
# worker protocol's catch-all `default`, PlutoBackend.set_condition dropped the unrecognised key,
# and the link stayed on one channel while every tool reported success and the following jammer
# chased a schedule that was never flown. These pin each link of that chain.

def test_worker_protocol_refuses_what_the_far_end_cannot_decode():
    from gr_autopilot.hardware.protocol import dumps
    with pytest.raises(TypeError):
        dumps({"condition": {"hop_plan": HopPlan(CHANS, 4.0)}})


def test_pluto_declares_that_it_cannot_hop_or_take_relative_power():
    from gr_autopilot.link.pluto import PlutoBackend
    assert "center_freq_hz" in PlutoBackend.applies_condition
    assert "hop_plan" not in PlutoBackend.applies_condition
    assert "tx_power_db" not in PlutoBackend.applies_condition


def test_hopping_backend_adds_hop_plan_to_what_the_inner_backend_applies():
    inner = MockHoppingBackend()
    inner.applies_condition = frozenset({"center_freq_hz", "tx_atten_db"})
    be = HoppingBackend(inner)
    assert "hop_plan" in be.applies_condition and "center_freq_hz" in be.applies_condition


def test_service_refuses_a_hop_plan_a_backend_would_silently_drop():
    """The whole point: a backend that cannot hop must REFUSE, not report success."""
    from gr_autopilot.tools.service import AutopilotService, ToolError

    class Deaf(MockHoppingBackend):
        applies_condition = frozenset({"center_freq_hz"})       # no hop_plan
        owns_channel = True

        def set_condition(self, **cond):
            cond.pop("hop_plan", None)                          # drops it, exactly as Pluto did
            super().set_condition(**cond)

    svc = AutopilotService(backend=Deaf(), channel={})
    svc.start_experiment("t", "")
    with pytest.raises(ToolError) as e:
        svc.set_hop_plan(list(CHANS), 4.0)
    assert e.value.code == "not_supported"
    assert svc._agent_rf.get("hop_plan") is None                 # and nothing was recorded


def test_hopping_backend_restores_the_lo_without_an_observable_inner_tuning():
    """WorkerBackend holds the radio in a child process and exposes no center_freq_hz; the
    decorator has to remember the tuning itself or the LO is left on the last hop channel."""
    from gr_autopilot.link.backend import LinkBackend as _LB

    class Opaque(_LB):                                           # shaped like WorkerBackend
        name, owns_channel = "opaque", True

        def __init__(self):
            self._sim, self.tuned = MockHoppingBackend(), []

        def set_condition(self, **cond):                         # swallows it into the child proc
            if cond.get("center_freq_hz") is not None:
                self.tuned.append(float(cond["center_freq_hz"]))
            self._sim.set_condition(**cond)

        def run_link(self, params):
            return self._sim.run_link(params)

    inner = Opaque()
    assert not hasattr(inner, "condition")
    be = HoppingBackend(inner)
    be.set_condition(center_freq_hz=CHANS[1])
    be.set_condition(hop_plan=HopPlan(CHANS, 4.0))
    be.run_link(_LP(modulation="qpsk", n_payload_bits=8000, sps=8))
    assert be.center_freq_hz == CHANS[1]                         # back where it started
    assert inner.tuned[-1] == CHANS[1]                           # and the radio was told so


def test_hop_records_grade_each_dwell_separately():
    """Per-dwell rows, sliced on ACTUAL graded lengths, so one dwell's errors stay its own."""
    from gr_autopilot.tools.service import AutopilotService

    inner = MockHoppingBackend()
    inner.jammed_channels = {CHANS[0]}                           # exactly one of four is hit
    svc = AutopilotService(backend=HoppingBackend(inner), channel={})
    svc.start_experiment("t", "")
    svc.build_flowgraph({"structure_id": "q", "modulation": "qpsk",
                         "tx_chain": ["qpsk_mod", "rrc_pulse_shape"],
                         "rx_chain": ["agc", "rrc_matched_filter", "symbol_sync",
                                      "costas_carrier", "qpsk_demod"]})
    svc.set_hop_plan(list(CHANS), 4.0)
    svc.run_flowgraph(n_bits=8000)
    hop = svc.get_metrics()["hop"]
    assert hop["n_dwells"] == 4 and len(hop["dwells"]) == 4
    assert [d["center_freq_hz"] for d in hop["dwells"]] == list(CHANS)
    hit = [d for d in hop["dwells"] if d["BER"] > 1e-2]
    assert [d["center_freq_hz"] for d in hit] == [CHANS[0]]      # the jammed dwell, and only it
    # pooled == total errors / total bits, never the mean of the per-dwell ratios
    tot_b = sum(d["n_bits"] for d in hop["dwells"])
    tot_e = sum(d["n_errors"] for d in hop["dwells"])
    assert svc.get_metrics()["BER"] == pytest.approx(tot_e / tot_b, rel=1e-9)
    assert hop["dwell_s_requested"] == pytest.approx(0.25)
    assert hop["hop_rate_hz_requested"] == pytest.approx(4.0)
    assert hop["dwell_s_measured_mean"] is not None              # timed, not assumed


def test_a_non_hopped_run_carries_no_hop_block():
    """Experiments 1 and 2 never set a hop plan; get_metrics must look exactly as it did."""
    from gr_autopilot.tools.service import AutopilotService

    svc = AutopilotService(backend=HoppingBackend(MockHoppingBackend()), channel={})
    svc.start_experiment("t", "")
    svc.build_flowgraph({"structure_id": "q", "modulation": "qpsk",
                         "tx_chain": ["qpsk_mod", "rrc_pulse_shape"],
                         "rx_chain": ["agc", "rrc_matched_filter", "symbol_sync",
                                      "costas_carrier", "qpsk_demod"]})
    svc.run_flowgraph(n_bits=8000)
    assert set(svc.get_metrics()) == {"BER", "EVM", "SNR", "n_bits", "n_errors"}


def test_the_follower_is_told_the_channel_the_link_is_actually_on():
    """current_link_freq drives the following jammer. While a backend is physically hopping it
    must report the live dwell, not schedule arithmetic the link may not be obeying."""
    from gr_autopilot.tools.service import AutopilotService

    inner = MockHoppingBackend()
    be = HoppingBackend(inner)
    seen = []
    be.on_dwell = lambda ch: seen.append(be.live_channel_hz)
    svc = AutopilotService(backend=be, channel={})
    svc.start_experiment("t", "")
    svc.build_flowgraph({"structure_id": "q", "modulation": "qpsk",
                         "tx_chain": ["qpsk_mod", "rrc_pulse_shape"],
                         "rx_chain": ["agc", "rrc_matched_filter", "symbol_sync",
                                      "costas_carrier", "qpsk_demod"]})
    svc.set_center_freq(CHANS[1])
    assert svc.current_link_freq() == CHANS[1]
    svc.set_hop_plan(list(CHANS), 4.0)
    svc.run_flowgraph(n_bits=8000)
    assert svc.current_link_freq() == CHANS[1]                   # LO restored after the cycle
