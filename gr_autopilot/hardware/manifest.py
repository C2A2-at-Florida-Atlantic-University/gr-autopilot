"""Device capability manifests (spec §5.3).

Structured, agent-reasoned descriptions of what physical capability exists. The Pluto
manifest encodes VANILLA AD9363 firmware reality: a single TX and single RX channel,
325 MHz-3.8 GHz tuning, and quirks (not hacked to AD9364, USB-2 sustained-rate ceiling,
DC/LO leakage, two independent 25 ppm clocks).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class DeviceManifest:
    device_id: str
    driver: str
    uri: str
    serial: str
    tuning_range_hz: list = field(default_factory=list)
    sample_rate_range: list = field(default_factory=list)
    usable_bandwidth_hz: Any = None
    tx: dict = field(default_factory=dict)
    rx: dict = field(default_factory=dict)
    clock: dict = field(default_factory=dict)
    duplex: str = "full"
    quirks: list = field(default_factory=list)
    #: True when this manifest describes no physical radio. Carried into ``list_devices`` so a
    #: simulated inventory can never be mistaken for attached hardware (the failure this field
    #: exists to prevent: the daemon ran real radios while serving mock manifests, and the agent
    #: reasoned about the wrong chip's tuning limits and clock).
    simulated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# Hand-curated quirks for a VANILLA (un-hacked) ADALM-Pluto (AD9363). Spec §5.3: these
# cannot be auto-detected; they carry narrative weight and steer the agent's reasoning.
VANILLA_PLUTO_QUIRKS = [
    "chip:AD9363 (NOT hacked to AD9364) -> single TX + single RX channel only",
    "tuning limited to 325 MHz-3.8 GHz on vanilla firmware (cannot go below 325 MHz)",
    "USB 2.0 sustained-sample-rate ceiling far below the 61.44 Msps chip rate; keep rates modest",
    "stock oscillator +/-25 ppm -> ~+/-120 kHz CFO between two units at 2.4 GHz (add coarse CFO correction)",
    "DC offset / LO leakage present -> motivates offset tuning and DC blocking",
]

# A Pluto hacked to present as AD9364 (e.g. bench unit pluto2). Same single-channel radio,
# but the extended tuning range unlocks — read from the live device, never assumed.
HACKED_PLUTO_QUIRKS = [
    "chip:AD9364 (hacked from AD9363) -> extended tuning 70 MHz-6 GHz; still single TX + single RX",
    "USB 2.0 sustained-sample-rate ceiling far below the 61.44 Msps chip rate; keep rates modest",
    "free-running oscillator -> kHz-scale CFO vs another unit (needs carrier recovery)",
    "DC offset / LO leakage present -> motivates offset tuning and DC blocking",
]


def pluto_manifest(device_id: str, uri: str, serial: str,
                   simulated: bool = False) -> DeviceManifest:
    """A vanilla-AD9363 ADALM-Pluto capability manifest (spec §5.3 example)."""
    return DeviceManifest(
        device_id=device_id,
        driver="iio",
        uri=uri,
        serial=serial,
        tuning_range_hz=[325e6, 3.8e9],
        sample_rate_range=[521e3, 61.44e6],
        usable_bandwidth_hz=None,
        tx={"channels": 1, "gain_range_db": [-89.0, 0.0]},
        rx={"channels": 1, "gain_modes": ["manual", "slow_attack", "fast_attack"],
            "gain_range_db": [-3.0, 71.0]},
        clock={"internal_ppm": 25, "ext_ref": False},
        duplex="full",
        quirks=list(VANILLA_PLUTO_QUIRKS),
        simulated=simulated,
    )
