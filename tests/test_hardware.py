"""Hardware registry tests (mock backend; no radios). Includes the integrity-split checks."""
import pytest

from gr_autopilot.hardware import (
    ClaimError,
    ClaimRegistry,
    Role,
    detect_devices,
    probe_device,
)
from gr_autopilot.hardware import detect as _detect

# Real ``iio_info -s`` / ``iio_attr -C`` samples from the bench (each unit shows up on both
# usb and ip; the two units are an asymmetric AD9364/AD9363A pair).
_SCAN = """Available contexts:
\t0: 0456:b673 (PlutoSDR), serial=AAA111 [usb:3.11.5]
\t1: 0456:b673 (PlutoSDR), serial=BBB222 [usb:3.9.5]
\t2: 192.168.2.1 (Z7010-AD9364), serial=AAA111 [ip:pluto2.local]
\t3: 192.168.3.1 (Z7010-AD9363A), serial=BBB222 [ip:pluto3.local]
"""
_ATTRS_AD9364 = """IIO context with 9 attributes:
hw_model: Analog Devices PlutoSDR Rev.C (Z7010-AD9364)
hw_serial: 10447318ac0f000301000700a56917726b
fw_version: v0.39
ad9361-phy,xo_correction: 40000145
ad9361-phy,model: ad9364
uri: ip:192.168.2.1
ip,ip-addr: 192.168.2.1
"""
# The second unit differs in chip, address, clock trim AND serial. The two real serials share
# the batch prefix "104473", which is why a device id derived from a serial prefix collides.
_ATTRS_AD9363A = _ATTRS_AD9364.replace("ad9364", "ad9363a").replace(
    "192.168.2.1", "192.168.3.1").replace("40000145", "40000119").replace(
    "10447318ac0f000301000700a56917726b", "10447392da110010f0ff2300ef2af86e6a")


def test_detect_returns_two_vanilla_plutos():
    devs = detect_devices("mock")
    assert [d.device_id for d in devs] == ["pluto-a", "pluto-b"]
    a = devs[0]
    assert a.tuning_range_hz == [325e6, 3.8e9]        # vanilla AD9363 range
    assert a.tx["channels"] == 1 and a.rx["channels"] == 1  # single channel each
    assert a.clock["internal_ppm"] == 25
    assert any("NOT hacked" in q for q in a.quirks)
    assert any("120 kHz" in q for q in a.quirks)


def test_probe_ok():
    r = probe_device("pluto-a")
    assert r.ok and r.n_samples > 0


def test_scan_dedups_by_serial_prefers_ip():
    # Two physical units, each on usb+ip -> two URIs, the ip form winning.
    assert _detect._parse_scan_uris(_SCAN) == ["ip:pluto2.local", "ip:pluto3.local"]


def test_context_attrs_parse():
    a = _detect._parse_context_attrs(_ATTRS_AD9364)
    assert a["ad9361-phy,model"] == "ad9364"
    assert a["hw_serial"].startswith("10447318")
    assert a["ip,ip-addr"] == "192.168.2.1"


def test_manifest_reads_live_asymmetry(monkeypatch):
    # The hacked AD9364 and the vanilla AD9363A must be told apart from live attributes.
    monkeypatch.setattr(_detect, "_context_attrs",
                        lambda uri: _detect._parse_context_attrs(_ATTRS_AD9364))
    hacked = _detect._manifest_from_uri("ip:pluto2.local")
    assert hacked.device_id == "pluto2"
    assert hacked.uri == "ip:192.168.2.1"
    assert hacked.tuning_range_hz == [70e6, 6e9]        # extended range
    assert hacked.clock["chip_model"] == "ad9364"
    assert hacked.clock["xo_correction"] == 40000145
    assert any("AD9364" in q and "hacked" in q for q in hacked.quirks)

    monkeypatch.setattr(_detect, "_context_attrs",
                        lambda uri: _detect._parse_context_attrs(_ATTRS_AD9363A))
    vanilla = _detect._manifest_from_uri("ip:pluto3.local")
    assert vanilla.tuning_range_hz == [325e6, 3.8e9]     # vanilla range
    assert any("NOT hacked" in q for q in vanilla.quirks)


def test_detect_real_scans_and_reads(monkeypatch):
    monkeypatch.setattr(_detect, "_scan_pluto_uris", lambda: ["ip:pluto3.local"])
    monkeypatch.setattr(_detect, "_context_attrs",
                        lambda uri: _detect._parse_context_attrs(_ATTRS_AD9363A))
    devs = detect_devices("pluto")
    assert len(devs) == 1 and devs[0].clock["chip_model"] == "ad9363a"


def test_agent_can_claim_radio_roles():
    reg = ClaimRegistry({"pluto-a", "pluto-b"})
    reg.claim_device("pluto-a", Role.TRANSMITTER)
    reg.claim_device("pluto-b", Role.RECEIVER)
    assert len(reg.list_claims()) == 2


def test_agent_cannot_claim_grader():
    reg = ClaimRegistry({"pluto-a", "pluto-b"})
    with pytest.raises(ClaimError, match="grader|framework"):
        reg.claim_device("pluto-b", Role.GRADER)
    with pytest.raises(ClaimError, match="grader|framework"):
        reg.claim_device("pluto-b", Role.MONITOR)


def test_agent_cannot_claim_unknown_role():
    reg = ClaimRegistry({"pluto-a"})
    with pytest.raises(ClaimError, match="not agent-claimable"):
        reg.claim_device("pluto-a", "wizard")


def test_grader_and_receiver_coexist_on_rx_device():
    reg = ClaimRegistry({"pluto-a", "pluto-b"})
    reg.framework_reserve("pluto-b", Role.GRADER)   # framework owns the grader on the RX device
    # The agent may still claim the RECEIVER role on that device (it controls the receiver DSP)...
    reg.claim_device("pluto-b", Role.RECEIVER)
    # ...but the grader role itself stays off-limits to the agent (role-level integrity).
    with pytest.raises(ClaimError, match="grader|framework"):
        reg.claim_device("pluto-b", Role.GRADER)
    # ...and the framework's grader claim cannot be duplicated.
    with pytest.raises(ClaimError, match="already claimed"):
        reg.framework_reserve("pluto-b", Role.GRADER)


def test_double_claim_rejected():
    reg = ClaimRegistry({"pluto-a"})
    reg.claim_device("pluto-a", Role.TRANSMITTER)
    with pytest.raises(ClaimError, match="already claimed"):
        reg.claim_device("pluto-a", Role.TRANSMITTER)


def test_unknown_device_rejected():
    reg = ClaimRegistry({"pluto-a"})
    with pytest.raises(ClaimError, match="unknown device"):
        reg.claim_device("pluto-z", Role.TRANSMITTER)


# --- role binding by URI (the mock-manifest regression) ---------------------------------------
#
# The daemon once ran real radios while the device tools served mock manifests, so the agent
# reasoned about the wrong chip's tuning range and clock. These cover the three ways the obvious
# repair goes wrong, each of them silent.

def _attrs_by_uri(uri):
    """Both bench units, answering on either their mDNS name or their numeric address."""
    if "192.168.2.1" in uri or "pluto2" in uri:
        return _detect._parse_context_attrs(_ATTRS_AD9364)     # the hacked TX
    return _detect._parse_context_attrs(_ATTRS_AD9363A)        # the vanilla RX


def test_bench_binds_roles_to_the_named_radios(monkeypatch):
    monkeypatch.setattr(_detect, "_context_attrs", _attrs_by_uri)
    tx, rx = _detect.detect_bench("ip:192.168.2.1", "ip:192.168.3.1")
    # Logical role ids, real identity underneath.
    assert (tx.device_id, rx.device_id) == ("pluto-a", "pluto-b")
    assert tx.clock["chip_model"] == "ad9364" and tx.tuning_range_hz == [70e6, 6e9]
    assert rx.clock["chip_model"] == "ad9363a" and rx.tuning_range_hz == [325e6, 3.8e9]
    assert tx.uri == "ip:192.168.2.1" and rx.uri == "ip:192.168.3.1"
    assert not tx.simulated and not rx.simulated


def test_bench_does_not_invert_roles_on_discovery_order(monkeypatch):
    # A bare scan lists this bench RX-first; binding by URI must ignore that entirely.
    monkeypatch.setattr(_detect, "_context_attrs", _attrs_by_uri)
    monkeypatch.setattr(_detect, "_scan_pluto_uris",
                        lambda: ["ip:pluto3.local", "ip:pluto2.local"])
    assert [d.device_id for d in detect_devices("pluto")] == ["pluto3", "pluto2"]  # scan order
    tx, _rx = _detect.detect_bench("ip:192.168.2.1", "ip:192.168.3.1")
    assert tx.clock["chip_model"] == "ad9364"   # the transmitter, not whatever answered first


def test_bench_rejects_one_radio_in_both_roles(monkeypatch):
    # Same unit under two URI spellings: caught by serial, not by string comparison.
    monkeypatch.setattr(_detect, "_context_attrs", _attrs_by_uri)
    with pytest.raises(RuntimeError, match="same radio"):
        _detect.detect_bench("ip:pluto2.local", "ip:192.168.2.1")


def test_mock_manifests_declare_themselves_simulated():
    assert all(d.simulated for d in detect_devices("mock"))


def test_real_probe_reports_liveness_and_never_invents_a_noise_floor(monkeypatch):
    import gr_autopilot.hardware.probe as _probe
    monkeypatch.setattr(_probe, "probe_uri", lambda uri, **kw: {"alive": True, "temp_c": 35.1})
    r = _probe.probe_device("pluto-a", backend="pluto", uri="ip:192.168.2.1")
    assert r.ok and r.temp_c == 35.1 and not r.simulated
    assert r.noise_floor_dbfs is None            # never fabricated on a live radio
    assert "buffers" in r.note

    monkeypatch.setattr(_probe, "probe_uri",
                        lambda uri, **kw: {"alive": False, "reason": "no answer"})
    dead = _probe.probe_device("pluto-a", backend="pluto", uri="ip:192.168.2.1")
    assert not dead.ok and "no answer" in dead.note


def test_real_probe_needs_a_uri():
    with pytest.raises(ValueError, match="uri"):
        probe_device("pluto-a", backend="pluto")


def test_mock_probe_still_marks_its_noise_floor_synthetic():
    r = probe_device("pluto-a")
    assert r.ok and r.simulated and r.noise_floor_dbfs == -72.0
