"""Hardware registry (spec §5.3): detect -> describe -> claim.

The agent never assumes hardware. Devices are enumerated into structured capability
manifests the agent reasons over; roles are claimed with the scoring-integrity split
enforced (the agent configures radios but cannot claim the framework-owned grader).

A MOCK detector keeps the whole tool surface and claim logic testable with no radios attached;
``detect_devices('pluto')`` scans attached units with libiio, and ``detect_bench`` binds the
TX and RX roles to two named URIs. Manifests carry ``simulated`` so a mock inventory is never
mistaken for attached hardware.
"""
from gr_autopilot.hardware.claim import (
    AGENT_CLAIMABLE_ROLES,
    FRAMEWORK_ROLES,
    ClaimError,
    ClaimRegistry,
    Role,
)
from gr_autopilot.hardware.detect import detect_bench, detect_devices
from gr_autopilot.hardware.manifest import DeviceManifest, pluto_manifest
from gr_autopilot.hardware.probe import probe_device

__all__ = [
    "AGENT_CLAIMABLE_ROLES",
    "FRAMEWORK_ROLES",
    "ClaimError",
    "ClaimRegistry",
    "Role",
    "detect_bench",
    "detect_devices",
    "DeviceManifest",
    "pluto_manifest",
    "probe_device",
]
