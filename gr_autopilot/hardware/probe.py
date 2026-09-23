"""Device health check (spec §5.3).

``probe_device`` answers one question for the agent: is this radio still there, so that "my
flowgraph is wrong" can be told apart from "the radio fell off the bus" (Pluto flakiness is real).

On the mock backend it returns a healthy baseline with a synthetic noise floor, and says so.

On real hardware it does NOT report a noise floor, and that is deliberate. Measuring one means
capturing samples, and a capture opened over a second connection while the worker holds the radios
succeeds -- the device server multiplexes it -- whilst silently taking two whole buffers away from
the capture in progress (see :mod:`gr_autopilot.hardware.health`). A probe that quietly corrupts
the measurement it is meant to be watching is worse than no probe, and a probe that invents the
number is worse still. So the real path reports what an attribute read can establish honestly --
liveness and die temperature -- and reports the noise floor as unavailable, with the reason.
"""
from __future__ import annotations

from dataclasses import dataclass

from gr_autopilot.hardware.health import probe_uri

#: Why the real backend returns no noise floor. Surfaced to the agent so the absence reads as a
#: deliberate limit of a live probe rather than a broken device.
NO_NOISE_FLOOR_NOTE = (
    "noise floor unavailable on a live radio: sampling it over a second connection would steal "
    "buffers from the capture in progress"
)


@dataclass
class ProbeResult:
    device_id: str
    ok: bool
    noise_floor_dbfs: float | None
    n_samples: int
    note: str = ""
    simulated: bool = False
    temp_c: float | None = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "ok": self.ok,
            "noise_floor_dbfs": self.noise_floor_dbfs,
            "n_samples": self.n_samples,
            "note": self.note,
            "simulated": self.simulated,
            "temp_c": self.temp_c,
        }


def probe_device(device_id: str, backend: str = "mock", n_samples: int = 4096,
                 uri: str | None = None) -> ProbeResult:
    """Health-check a device. Returns a clean baseline provenance line for the ledger."""
    if backend == "mock":
        return ProbeResult(
            device_id=device_id,
            ok=True,
            noise_floor_dbfs=-72.0,
            n_samples=n_samples,
            note="mock probe (no hardware): baseline OK",
            simulated=True,
        )
    if backend not in ("pluto", "iio", "real"):
        raise ValueError(f"unknown probe backend {backend!r}")
    if not uri:
        raise ValueError(f"probing {device_id!r} on the {backend!r} backend needs its IIO uri")
    health = probe_uri(uri)
    alive = bool(health.get("alive"))
    return ProbeResult(
        device_id=device_id,
        ok=alive,
        noise_floor_dbfs=None,
        n_samples=0,
        note=NO_NOISE_FLOOR_NOTE if alive else f"device not answering: {health.get('reason', '')}",
        simulated=False,
        temp_c=health.get("temp_c"),
    )
