"""Role claiming with the scoring-integrity split enforced at the hardware layer (spec §5.3).

Roles are an open set (spec §6). The framework pre-claims whatever the frozen measurement
path needs; the agent may claim radio roles (transmitter/receiver/interferer/adversary) but
CANNOT claim the framework-owned grader (the privileged ``monitor``/``grader`` role). This
is the hardware-layer enforcement of "the agent controls the radio; the framework owns the
grader" (spec §2).
"""
from __future__ import annotations

from dataclasses import dataclass


class Role:
    TRANSMITTER = "transmitter"
    RECEIVER = "receiver"
    INTERFERER = "interferer"      # Stage 2
    ADVERSARY = "adversary"        # Stage 3
    MONITOR = "monitor"            # framework-owned passive observer (houses the grader)
    GRADER = "grader"              # the frozen measurement path


# Roles the agent is allowed to claim vs. roles reserved for the framework.
AGENT_CLAIMABLE_ROLES = frozenset({Role.TRANSMITTER, Role.RECEIVER, Role.INTERFERER, Role.ADVERSARY})
FRAMEWORK_ROLES = frozenset({Role.MONITOR, Role.GRADER})


class ClaimError(Exception):
    """Raised when a claim violates ownership or the integrity split."""


@dataclass(frozen=True)
class Claim:
    device_id: str
    role: str
    owner: str  # "agent" | "framework"


class ClaimRegistry:
    """Tracks device role assignments and enforces the integrity split."""

    def __init__(self, known_devices: set[str] | None = None):
        self.known_devices = set(known_devices or [])
        self._claims: dict[tuple[str, str], Claim] = {}  # (device_id, role) -> Claim

    def register_devices(self, device_ids) -> None:
        self.known_devices.update(device_ids)

    def framework_reserve(self, device_id: str, role: str = Role.GRADER) -> Claim:
        """The framework pre-claims the grader/monitor path. Cannot be overridden by the agent."""
        return self._claim(device_id, role, owner="framework")

    def claim_device(self, device_id: str, role: str, owner: str = "agent") -> Claim:
        """Claim a device in a role. Agents are barred from framework-owned roles."""
        if owner == "agent" and role in FRAMEWORK_ROLES:
            raise ClaimError(
                f"agent cannot claim framework-owned role {role!r} "
                f"(the grader is framework-reserved; the agent controls the radio, not the grader)"
            )
        if owner == "agent" and role not in AGENT_CLAIMABLE_ROLES:
            raise ClaimError(f"role {role!r} is not agent-claimable (allowed: {sorted(AGENT_CLAIMABLE_ROLES)})")
        return self._claim(device_id, role, owner=owner)

    def _claim(self, device_id: str, role: str, owner: str) -> Claim:
        if self.known_devices and device_id not in self.known_devices:
            raise ClaimError(f"unknown device {device_id!r}")
        # Integrity is enforced per ROLE, not per device: the framework's grader and the
        # agent's receiver co-exist on the RX device (the agent controls the receiver DSP;
        # the framework owns the co-located measurement). Only the grader ROLE is off-limits.
        key = (device_id, role)
        if key in self._claims:
            held = self._claims[key]
            raise ClaimError(f"{device_id!r} already claimed for role {role!r} by {held.owner}")
        claim = Claim(device_id=device_id, role=role, owner=owner)
        self._claims[key] = claim
        return claim

    def release(self, device_id: str, role: str) -> None:
        self._claims.pop((device_id, role), None)

    def who_has(self, device_id: str) -> list[Claim]:
        return [c for (dev, _), c in self._claims.items() if dev == device_id]

    def list_claims(self) -> list[dict]:
        return [{"device_id": c.device_id, "role": c.role, "owner": c.owner}
                for c in self._claims.values()]
