"""AutopilotService tests: the §8 tool surface + integrity split (no hardware, no MCP SDK)."""
import pytest

from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.tools import AutopilotService, ToolError


def make(tmp_path, **channel):
    return AutopilotService(channel=channel or {"es_n0_db": 12.0},
                            ledger_path=tmp_path / "session.jsonl", confirm_bits=200_000)


# ---- discovery --------------------------------------------------------------

def test_discovery_tools(tmp_path):
    svc = make(tmp_path)
    assert len(svc.list_blocks()) > 100
    assert any(s["name"] == "qpsk_mod" for s in svc.list_skills())
    devs = svc.list_devices()
    assert [d["device_id"] for d in devs] == ["pluto-a", "pluto-b"]
    assert svc.probe_device("pluto-a")["ok"]
    assert "attenuator" in svc.get_rf_path_config()["path"]


def test_hidden_channel_routes_by_backend(tmp_path):
    # Sim backend (owns_channel=False): the hidden channel is handed to LinkParams.
    svc = make(tmp_path, es_n0_db=9.0)
    assert svc._channel_kwargs() == {"es_n0_db": 9.0}

    # A hardware-style backend owns its channel: set_condition receives it and the params
    # carry nothing about the condition (so it can never leak through the tool surface).
    class FakeHW:
        name, owns_channel = "fake-hw", True

        def __init__(self):
            self.applied = None

        def set_condition(self, **c):
            self.applied = dict(c)

        def run_link(self, params):  # pragma: no cover - not exercised here
            raise NotImplementedError

    hw = FakeHW()
    svc2 = AutopilotService(backend=hw, channel={"tx_atten_db": 40.0, "rx_gain_db": 35.0},
                            ledger_path=tmp_path / "hw.jsonl")
    assert svc2._channel_kwargs() == {}
    assert hw.applied == {"tx_atten_db": 40.0, "rx_gain_db": 35.0}


# ---- M3 exit: drive the whole loop through the tools ------------------------

def test_build_run_measure_loop(tmp_path):
    svc = make(tmp_path, es_n0_db=12.0)
    spec = FlowgraphSpec.link("qpsk").to_dict()
    assert svc.validate(spec)["ok"]
    assert svc.build_flowgraph(spec)["structure_id"] == "qpsk_link"
    svc.run_flowgraph(200_000)
    m = svc.get_metrics()
    assert m["BER"] <= 1e-3           # QPSK at 12 dB comfortably meets target
    assert m["n_bits"] > 0
    # framework logged the iteration to the ledger
    assert len(svc.read_edit_ledger(compact=False)) == 1


def test_validate_and_build_reject_bad_spec(tmp_path):
    svc = make(tmp_path)
    bad = FlowgraphSpec.link("qpsk")
    bad.rx_chain[-1] = "bpsk_demod"
    assert not svc.validate(bad.to_dict())["ok"]
    with pytest.raises(ToolError):
        svc.build_flowgraph(bad.to_dict())


def test_get_metrics_requires_a_run(tmp_path):
    svc = make(tmp_path)
    with pytest.raises(ToolError, match="no_result"):
        svc.get_metrics()


# ---- integrity split --------------------------------------------------------

def test_agent_claims_radios_but_not_grader(tmp_path):
    svc = make(tmp_path)
    assert svc.claim_device("pluto-a", "transmitter")["role"] == "transmitter"
    assert svc.claim_device("pluto-b", "receiver")["role"] == "receiver"   # controls RX DSP
    with pytest.raises(ToolError, match="claim_denied"):
        svc.claim_device("pluto-b", "grader")                              # but not the grader


def test_status_does_not_leak_the_hidden_snr(tmp_path):
    svc = make(tmp_path, es_n0_db=17.3, phase_offset_rad=0.4)
    svc.build_flowgraph(FlowgraphSpec.link("qpsk").to_dict())
    svc.run_flowgraph(50_000)
    status = svc.get_status()
    metrics = svc.get_metrics()
    blob = str(status) + str(metrics)
    assert "17.3" not in blob and "es_n0" not in blob   # the SNR setting is never exposed


# ---- BO inner loop tool -----------------------------------------------------

def test_bo_tool_tunes_phase_correction(tmp_path):
    svc = make(tmp_path, es_n0_db=18.0, phase_offset_rad=0.5)
    svc.build_flowgraph(FlowgraphSpec.link("16qam").to_dict())
    out = svc.start_bo_run(["phase_correction_rad"], [[-0.7854, 0.7854]], budget=20)
    assert out["best_params"]["phase_correction_rad"] == pytest.approx(0.5, abs=0.12)
    # tuning persists; the tuned 16-QAM link now meets target where uncorrected it would not
    svc.run_flowgraph(200_000)
    assert svc.get_metrics()["BER"] <= 1e-3


def test_bo_rejects_untunable_param(tmp_path):
    svc = make(tmp_path)
    svc.build_flowgraph(FlowgraphSpec.link("qpsk").to_dict())
    with pytest.raises(ToolError, match="untunable_param"):
        svc.start_bo_run(["gain"], [[0, 1]], budget=4)
