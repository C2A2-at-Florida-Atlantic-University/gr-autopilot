"""The declared bench, and the cross-check that makes device detection falsifiable.

The property under test is the one whose absence let the daemon serve fabricated device manifests
while transmitting on real radios: with a declaration, a disagreement between what is claimed and
what the hardware reports is *detectable*.
"""
import pytest

from gr_autopilot.hardware.manifest import DeviceManifest
from gr_autopilot.hardware.topology import (
    TopologyError,
    load_topology,
    verify_identity,
)

BENCH = """
version: 1
name: test-bench
attestation: {medium: coax, antennas_attached: false, operator: t@example.com, date: 2026-09-07}
devices:
  pluto2:
    kind: pluto
    role: transmitter
    iio_uri: "ip:192.168.2.1"
    chip: ad9364
    tuning_range_hz: [70.0e6, 6.0e9]
    serial: "AAA"
  pluto3:
    kind: pluto
    role: receiver
    iio_uri: "ip:192.168.3.1"
    chip: ad9363a
    tuning_range_hz: [325.0e6, 3.8e9]
    serial: "BBB"
  hackrf1: {kind: hackrf, role: jammer, serial: "CCC"}
path:
  - {from: pluto2.rf_out, attenuator_db: 20, to: combiner.in1}
  - {from: combiner.out, insertion_loss_db: 3.5, to: pluto3.rf_in}
"""


def _write(tmp_path, text, name="bench.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def _manifest(serial, chip, tuning, device_id="x"):
    return DeviceManifest(device_id=device_id, driver="iio", uri="ip:1.2.3.4", serial=serial,
                          tuning_range_hz=list(tuning), clock={"chip_model": chip})


@pytest.fixture
def topo(tmp_path):
    return load_topology(_write(tmp_path, BENCH))


# --- what the declaration derives -------------------------------------------------------------

def test_roles_and_hash(topo):
    assert topo.tx.name == "pluto2" and topo.rx.name == "pluto3"
    assert topo.jammer.name == "hackrf1" and topo.jammer.kind == "hackrf"
    assert len(topo.sha256) == 64


def test_link_band_is_the_intersection_not_the_transmitters_range(topo):
    # The hacked TX reaches 70 MHz; the vanilla RX cannot hear below 325 MHz. The link floor is
    # the receiver's, and getting this backwards is the classic asymmetric-bench mistake.
    assert topo.link_band_hz == [325e6, 3.8e9]


def test_the_agent_facing_path_carries_structure_but_no_path_loss(topo):
    cfg = topo.rf_path_config()
    assert "attenuator" in cfg["path"] and "combiner" in cfg["path"]
    assert cfg["medium"] == "coax" and cfg["link_band_hz"] == [325e6, 3.8e9]
    # Path loss is one subtraction away from the SNR the agent must not be told.
    assert not any("loss" in k for k in cfg)
    assert "3.5" not in str(cfg)


# --- constraints: refuse what the framework cannot do -----------------------------------------

def test_two_transmitters_are_refused(tmp_path):
    bad = BENCH.replace("role: receiver", "role: transmitter")
    with pytest.raises(TopologyError, match="role 'transmitter'"):
        load_topology(_write(tmp_path, bad))


def test_an_unsupported_device_kind_is_refused(tmp_path):
    bad = BENCH.replace("kind: hackrf", "kind: rtlsdr")
    with pytest.raises(TopologyError, match="cannot drive"):
        load_topology(_write(tmp_path, bad))


def test_a_device_cannot_hold_a_role_its_kind_cannot_fill(tmp_path):
    # A HackRF is transmit-only in this framework; it cannot be the graded receiver.
    bad = BENCH.replace("hackrf1: {kind: hackrf, role: jammer", "hackrf1: {kind: hackrf, role: receiver")
    with pytest.raises(TopologyError, match="cannot hold"):
        load_topology(_write(tmp_path, bad))


def test_one_radio_cannot_hold_two_roles(tmp_path):
    bad = BENCH.replace('serial: "BBB"', 'serial: "AAA"')
    with pytest.raises(TopologyError, match="one radio"):
        load_topology(_write(tmp_path, bad))


def test_non_overlapping_tuning_ranges_are_refused(tmp_path):
    bad = BENCH.replace("[325.0e6, 3.8e9]", "[8.0e9, 9.0e9]")
    with pytest.raises(TopologyError, match="do not overlap"):
        load_topology(_write(tmp_path, bad))


def test_an_unknown_version_is_refused_not_guessed_at(tmp_path):
    with pytest.raises(TopologyError, match="version"):
        load_topology(_write(tmp_path, BENCH.replace("version: 1", "version: 7")))


# --- tier A: the cross-check ------------------------------------------------------------------

def test_matching_hardware_produces_no_findings(topo):
    assert verify_identity(topo, {
        "transmitter": _manifest("AAA", "ad9364", [70e6, 6e9]),
        "receiver": _manifest("BBB", "ad9363a", [325e6, 3.8e9])}) == []


def test_the_mock_manifest_regression_is_now_detectable(topo):
    # Exactly the failure that went unnoticed: fabricated manifests served while real radios
    # transmitted. Against a declaration it is three findings, not silence.
    findings = verify_identity(topo, {
        "transmitter": _manifest("mock-A-0001", "ad9363", [325e6, 3.8e9]),
        "receiver": _manifest("BBB", "ad9363a", [325e6, 3.8e9])})
    fields = {f.field for f in findings}
    assert fields == {"serial", "chip", "tuning_range_hz"}
    assert "declared 'ad9364'" in str([str(f) for f in findings])


def test_a_missing_radio_is_a_finding(topo):
    findings = verify_identity(topo, {"transmitter": _manifest("AAA", "ad9364", [70e6, 6e9])})
    assert [f.field for f in findings] == ["presence"]


# --- tier C: the attestation gates the transmitter ---------------------------------------------

def test_a_cabled_bench_permits_a_jammer_only_with_nothing_radiating(tmp_path, topo):
    ok, why = topo.jammer_permitted()
    assert ok and "coax" in why

    antennas = load_topology(_write(tmp_path, BENCH.replace("antennas_attached: false",
                                                            "antennas_attached: true"), "b3.yaml"))
    assert antennas.jammer_permitted()[0] is False


def test_over_the_air_is_never_permitted(tmp_path):
    over_air = load_topology(_write(tmp_path, BENCH.replace("medium: coax", "medium: over_the_air"),
                                    "b2.yaml"))
    ok, why = over_air.jammer_permitted()
    assert not ok and "not a contained environment" in why


def test_anechoic_permits_a_jammer_only_when_shielded_is_explicitly_true(tmp_path):
    """Absorptive is not containing, so 'anechoic' alone must not open the interlock.

    A chamber lined with absorber stops reflections; only a screened boundary stops emission. The
    word "chamber" is not evidence of the second, so it is asked for and never inferred.
    """
    base = BENCH.replace("medium: coax", "medium: anechoic").replace(
        "antennas_attached: false", "antennas_attached: true")

    unstated = load_topology(_write(tmp_path, base, "a1.yaml"))
    ok, why = unstated.jammer_permitted()
    assert not ok and "not necessarily shielded" in why

    denied = load_topology(_write(tmp_path, base.replace(
        "antennas_attached: true", "antennas_attached: true, shielded: false"), "a2.yaml"))
    assert denied.jammer_permitted()[0] is False

    shielded = load_topology(_write(tmp_path, base.replace(
        "antennas_attached: true", "antennas_attached: true, shielded: true"), "a3.yaml"))
    ok, why = shielded.jammer_permitted()
    assert ok and "shielded" in why


def test_a_misspelt_medium_is_refused_rather_than_read_as_uncontained(tmp_path):
    with pytest.raises(TopologyError, match="attestation.medium"):
        load_topology(_write(tmp_path, BENCH.replace("medium: coax", "medium: coaxial"), "m.yaml"))


def test_no_jammer_declared(tmp_path):
    no_jammer = load_topology(_write(
        tmp_path, BENCH.replace('  hackrf1: {kind: hackrf, role: jammer, serial: "CCC"}\n', ""),
        "b4.yaml"))
    assert no_jammer.jammer_permitted() == (False, "no jammer is declared in the topology")


# --- the service honours it --------------------------------------------------------------------

def test_a_topology_fault_refuses_measurements(topo):
    from gr_autopilot.tools import AutopilotService, ToolError
    from gr_autopilot.hardware.topology import Finding
    svc = AutopilotService(topology=topo,
                           topology_findings=[Finding("pluto3", "chip", "ad9363a", "ad9364")])
    with pytest.raises(ToolError, match="topology_fault"):
        svc.run_flowgraph()


def test_retuning_outside_the_link_band_is_refused_with_a_reason(topo):
    from gr_autopilot.link.interference_sim import InterferenceSimBackend
    from gr_autopilot.tools import AutopilotService, ToolError
    svc = AutopilotService(backend=InterferenceSimBackend(), topology=topo)
    with pytest.raises(ToolError, match="out_of_band"):
        svc.set_center_freq(200e6)                      # below the receiver's 325 MHz floor
    with pytest.raises(ToolError, match="out_of_band"):
        svc.set_hop_plan([2.40e9, 5.0e9], 4.0)          # one channel out of reach
    assert svc.set_center_freq(2.4e9)["center_freq_hz"] == 2.4e9


def test_status_survives_the_http_boundary(topo):
    # YAML dates parse as datetime.date and serialise into a dict happily, then fail at the JSON
    # boundary — the same shape of bug as the numpy bool_ that once broke the sense result.
    import json
    json.dumps(topo.status([]))
    assert isinstance(topo.attestation["date"], str)


def test_the_real_bench_file_loads_and_describes_this_bench():
    topo = load_topology("config/bench.yaml")
    assert topo.tx.expect_chip == "ad9364" and topo.rx.expect_chip == "ad9363a"
    assert topo.link_band_hz == [325e6, 3.8e9]
    # The bench moved into an anechoic chamber on wireless links, so containment rests on the
    # chamber being screened rather than on copper. The operator has attested that it is.
    assert topo.attestation["medium"] == "anechoic"
    assert topo.attestation["antennas_attached"] is True
    assert topo.attestation["shielded"] is True
    assert topo.jammer_permitted() == (True, "attested shielded anechoic chamber")


# --- measured calibration travels with the bench, not with the code ---------------------------

def test_calibration_is_read_from_the_bench_file():
    """The gains the coaxial bench used were wrong in the chamber, and a hardcoded default cannot
    know that. Measured values live with the bench declaration that they describe."""
    topo = load_topology("config/bench.yaml")
    cal = topo.common["calibration"]
    assert cal["rx_gain_db"] == 45.0                       # measured optimum, not the old 40
    lo, hi = cal["rx_gain_window_db"]
    assert lo <= cal["rx_gain_db"] <= hi
