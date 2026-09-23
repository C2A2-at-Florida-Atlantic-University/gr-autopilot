"""Declared bench topology, and the cross-check that makes device detection falsifiable.

The problem this solves is not ergonomics. Until now there was exactly ONE source of truth about
what radios were attached -- whatever the detection layer said -- so when it was wrong, nothing
could contradict it: the daemon ran real radios for weeks while reporting fabricated manifests, and
the agent reasoned about the wrong chip's tuning limits. A declared topology is a second,
independent source. A receiver declared as an AD9363A that reports an AD9364 is now a *detectable
contradiction* rather than an invisible lie.

What can be known about a bench falls into three tiers, and keeping them apart is what makes this
tractable:

* **A -- electrically verifiable, automatically.** Which units are present and what they are:
  serial, chip model, firmware, clock trim, tuning range. Read over libiio in milliseconds without
  disturbing anything. This module checks tier A.
* **B -- verifiable by measurement.** Whether the transmitter is actually cabled to the receiver,
  whether the jammer actually reaches it, and what the end-to-end loss really is. Requires
  transmitting, so it must run through the radio worker while idle (a second connection that
  captures samples steals buffers from a capture in progress -- see :mod:`..hardware.health`).
  Not implemented here; the declaration records what tier B *would* confirm.
* **C -- not verifiable in principle.** That those are 20 dB pads and not 10s. That this is a cable
  and not an antenna. No measurement distinguishes them, so they are carried as a dated, named
  operator ATTESTATION rather than as an assumption buried in a source file -- and the jammer
  interlock reads it, because keying a transmitter into an antenna is the one mistake here that
  cannot be undone by editing a config.

The declaration is operator property, like the hidden channel condition. Nothing on the agent's
tool surface can write it, and the agent-facing projection (:meth:`Topology.rf_path_config`) is
structural only: what is connected to what, never the calibrated loss, which is one subtraction
away from the signal-to-noise ratio the agent is required not to be told.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

#: Device kinds this framework can drive, and the roles each can hold. A kind absent from here is
#: refused at load rather than half-supported: the DSP, the claim registry and the worker all
#: assume a radio they know. Adding a USRP later is one entry plus a detector.
SUPPORTED_KINDS = {
    "pluto": {"roles": {"transmitter", "receiver"}, "detect": "iio"},
    "hackrf": {"roles": {"jammer"}, "detect": "hackrf"},
}

#: Exactly one link in each direction, at most one jammer. The framework has one TX chain, one RX
#: chain and one grader; a second transmitter would have no defined meaning in the measurement.
ROLE_CARDINALITY = {"transmitter": (1, 1), "receiver": (1, 1), "jammer": (0, 1)}

#: How the bench is contained. The property that permits a transmitter is CONTAINMENT, not the
#: absence of antennas: a cabled path contains the signal in copper, a shielded enclosure contains
#: it in a Faraday boundary, and antennas are normal inside the second.
#:
#: "anechoic" and "shielded" are NOT the same thing and the distinction is the whole point here.
#: Anechoic means absorptive -- the walls kill reflections, which is a measurement property. It
#: says nothing about whether energy leaves the room. Only shielding contains it. A chamber that is
#: anechoic but unshielded radiates exactly as much as a bench in the open.
MEDIA = ("coax", "anechoic", "over_the_air")


class TopologyError(Exception):
    """The declaration is unusable: bad schema, bad cardinality, unsupported hardware.

    Fatal at startup. A bench that is not describable is not one to run experiments on.
    """


@dataclass
class DeviceDecl:
    """One declared device. The ``expect_*`` fields are the falsifiability hook: they are compared
    against what the hardware actually reports, and a mismatch is a fault, not a warning."""

    name: str
    kind: str
    role: str
    uri: str | None = None
    expect_serial: str | None = None
    expect_chip: str | None = None
    tuning_range_hz: list | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Finding:
    """One disagreement between what was declared and what the hardware reports."""

    device: str
    field: str
    declared: object
    detected: object

    def __str__(self) -> str:
        return (f"{self.device}.{self.field}: declared {self.declared!r}, "
                f"hardware reports {self.detected!r}")


@dataclass
class Topology:
    version: int
    name: str
    devices: dict
    attestation: dict = field(default_factory=dict)
    path: list = field(default_factory=list)
    common: dict = field(default_factory=dict)
    geometry: dict = field(default_factory=dict)
    source: str = ""
    sha256: str = ""

    # ---- roles ----------------------------------------------------------

    def by_role(self, role: str) -> DeviceDecl | None:
        for d in self.devices.values():
            if d.role == role:
                return d
        return None

    @property
    def tx(self) -> DeviceDecl:
        return self.by_role("transmitter")

    @property
    def rx(self) -> DeviceDecl:
        return self.by_role("receiver")

    @property
    def jammer(self) -> DeviceDecl | None:
        return self.by_role("jammer")

    # ---- derived capability ---------------------------------------------

    @property
    def link_band_hz(self) -> list | None:
        """The band the LINK can actually use: the intersection of the transmitter's and the
        receiver's tuning ranges, narrowed by any operator-declared limit.

        On an asymmetric bench this is the constraint people get wrong. A transmitter hacked to
        AD9364 reaches 70 MHz, but a vanilla AD9363A receiver cannot hear below 325 MHz, so the
        link floor is 325 MHz and a retune below it produces a confusing measurement failure
        rather than an honest refusal.
        """
        ranges = [d.tuning_range_hz for d in (self.tx, self.rx)
                  if d is not None and d.tuning_range_hz]
        declared = self.common.get("matched_tuning_range_hz")
        if declared:
            ranges.append(list(declared))
        if not ranges:
            return None
        lo = max(float(r[0]) for r in ranges)
        hi = min(float(r[1]) for r in ranges)
        return None if lo >= hi else [lo, hi]

    def jammer_permitted(self) -> tuple[bool, str]:
        """May a transmitter be keyed on this bench? Tier C: the operator's word, checked.

        Refuses unless a jammer is declared AND the attestation says the path is closed. This is
        the only interlock standing between a misconfigured bench and radiating an interfering
        signal, which is illegal in nearly every jurisdiction.
        """
        if self.jammer is None:
            return False, "no jammer is declared in the topology"
        att = self.attestation or {}
        if not att:
            return False, "topology has no operator attestation; a jammer needs one"
        medium = att.get("medium")
        if medium == "coax":
            if att.get("antennas_attached") is not False:
                return False, ("a coax path is only closed while nothing is radiating from it; "
                               "attestation does not state antennas_attached: false")
            return True, "attested closed coax path"
        if medium == "anechoic":
            # Absorptive is not containing. Asked for explicitly, and never inferred from the
            # word "chamber", because the mistake it prevents cannot be undone afterwards.
            if att.get("shielded") is not True:
                return False, (
                    "an anechoic chamber is absorptive, not necessarily shielded — it stops "
                    "reflections, not emission. Set attestation.shielded: true only if the "
                    "enclosure is a screened/Faraday room with the door closed and seals intact")
            return True, "attested shielded anechoic chamber"
        return False, (f"medium {medium!r} is not a contained environment; a transmitter may only "
                       f"be keyed into coax or a shielded enclosure")

    # ---- agent-facing projection ----------------------------------------

    def rf_path_config(self) -> dict:
        """What ``get_rf_path_config`` returns: STRUCTURE only.

        Deliberately omits any calibrated or nominal path loss. The agent is required to reason
        from framework-graded measurements without being told the channel condition, and a path
        loss it can subtract from a known transmit power is that condition in another form.
        """
        hops = []
        for hop in self.path:
            frm, to = hop.get("from", "?"), hop.get("to", "?")
            # Only the attenuator is named: the endpoints already say what the passive element
            # is, and its insertion loss is a path-loss term the agent must not be handed.
            mid = (f"{hop['attenuator_db']:g} dB attenuator -> "
                   if hop.get("attenuator_db") is not None else "")
            hops.append(f"{frm} -> {mid}{to}")
        return {
            "path": "; ".join(hops) if hops else f"{self.tx.name}.tx -> {self.rx.name}.rx",
            "medium": (self.attestation or {}).get("medium", "unknown"),
            "topology": self.name,
            "link_band_hz": self.link_band_hz,
        }

    @property
    def geometry_recorded(self) -> bool:
        """Is there enough recorded for someone else to rebuild this bench?

        Over the air, coupling is set by separation, antennas and orientation rather than by a pad
        value, so a result taken without them is reproducible only by whoever was standing in the
        room. Cabled paths are self-describing and need none of this.
        """
        if (self.attestation or {}).get("medium") == "coax":
            return True
        g = self.geometry
        return g.get("separation_m") is not None and g.get("antennas") is not None

    def reproducibility(self) -> dict:
        """What a third party would be missing, named field by field."""
        if (self.attestation or {}).get("medium") == "coax":
            return {"recorded": True, "missing": [], "note": "cabled path is self-describing"}
        g = self.geometry
        missing = [k for k in ("separation_m", "jammer_separation_m", "antennas")
                   if g.get(k) is None]
        return {"recorded": not missing, "missing": missing,
                "note": ("free-space coupling is set by geometry; without these a result is "
                         "reproducible only on this bench, by this operator")
                if missing else "geometry recorded"}

    def status(self, findings: list | None = None) -> dict:
        """Operator-side summary: the declaration, its hash, and any faults."""
        ok, why = self.jammer_permitted()
        return {
            "name": self.name, "version": self.version,
            "source": self.source, "sha256": self.sha256,
            "devices": {n: {"kind": d.kind, "role": d.role, "uri": d.uri,
                            "expect_serial": d.expect_serial, "expect_chip": d.expect_chip}
                        for n, d in self.devices.items()},
            "link_band_hz": self.link_band_hz,
            "attestation": self.attestation,
            "jammer_permitted": ok, "jammer_permitted_reason": why,
            "path": self.path,
            "reproducibility": self.reproducibility(),
            "geometry": self.geometry,
            # Three states, not two: never checked (null) is not the same as checked and clean.
            # Reporting an unchecked bench as "not verified" alongside an empty finding list reads
            # as a clean bill of health that nobody actually issued.
            "identity_verified": None if findings is None else not findings,
            "findings": None if findings is None else [str(f) for f in findings],
        }


# ---- loading + validation -------------------------------------------------


def _jsonable(obj):
    """Coerce YAML scalars that do not survive JSON.

    ``date: 2026-09-07`` parses as a :class:`datetime.date`, which serialises fine into a Python
    dict and then fails at the HTTP boundary -- the same shape of bug as the numpy ``bool_`` that
    once broke the sense result. Normalising once, here, keeps it out of every consumer.
    """
    import datetime
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (datetime.date, datetime.datetime, datetime.time)):
        return obj.isoformat()
    return obj


def load_topology(path) -> Topology:
    """Read and validate a bench declaration. Raises :class:`TopologyError` on anything unusable."""
    import yaml

    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise TopologyError(f"cannot read topology {p}: {exc}") from exc
    try:
        doc = yaml.safe_load(raw.decode("utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - yaml raises several types
        raise TopologyError(f"{p} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise TopologyError(f"{p}: expected a mapping at the top level")

    version = doc.get("version")
    if version != 1:
        raise TopologyError(
            f"{p}: unsupported topology version {version!r} (this build reads version 1). "
            "An unrecognised version is refused rather than guessed at.")

    devices = {}
    for name, d in (doc.get("devices") or {}).items():
        if not isinstance(d, dict):
            raise TopologyError(f"{p}: device {name!r} is not a mapping")
        kind, role = d.get("kind"), d.get("role")
        if kind not in SUPPORTED_KINDS:
            raise TopologyError(
                f"{p}: device {name!r} has kind {kind!r}, which this framework cannot drive "
                f"(supported: {', '.join(sorted(SUPPORTED_KINDS))})")
        if role not in ROLE_CARDINALITY:
            raise TopologyError(
                f"{p}: device {name!r} has role {role!r} (expected one of "
                f"{', '.join(sorted(ROLE_CARDINALITY))})")
        if role not in SUPPORTED_KINDS[kind]["roles"]:
            raise TopologyError(
                f"{p}: a {kind} cannot hold the {role!r} role in this framework "
                f"(it can be: {', '.join(sorted(SUPPORTED_KINDS[kind]['roles']))})")
        devices[name] = DeviceDecl(
            name=name, kind=kind, role=role,
            uri=d.get("iio_uri") or d.get("uri"),
            expect_serial=d.get("serial"),
            expect_chip=(d.get("chip") or "").lower() or None,
            tuning_range_hz=d.get("tuning_range_hz"),
            extra={k: v for k, v in d.items()
                   if k not in ("kind", "role", "iio_uri", "uri", "serial", "chip",
                                "tuning_range_hz")},
        )

    _check_cardinality(p, devices)
    _check_distinct_serials(p, devices)
    for role in ("transmitter", "receiver"):
        dev = next((d for d in devices.values() if d.role == role), None)
        if dev is not None and not dev.uri:
            raise TopologyError(f"{p}: the {role} {dev.name!r} needs an iio_uri")

    att = _jsonable(doc.get("attestation") or {})
    if att and att.get("medium") not in MEDIA:
        raise TopologyError(
            f"{p}: attestation.medium is {att.get('medium')!r}; expected one of "
            f"{', '.join(MEDIA)}. A misspelt medium would read as an uncontained bench.")

    topo = Topology(
        version=version, name=doc.get("name") or p.stem, devices=devices,
        attestation=att,
        path=_jsonable(doc.get("path") or []),
        common=_jsonable(doc.get("common") or {}),
        geometry=_jsonable(doc.get("geometry") or {}), source=str(p),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    if topo.link_band_hz is None:
        raise TopologyError(
            f"{p}: the declared transmitter and receiver tuning ranges do not overlap, so no link "
            "is possible on this bench")
    return topo


def _check_cardinality(p, devices: dict) -> None:
    for role, (lo, hi) in ROLE_CARDINALITY.items():
        n = sum(1 for d in devices.values() if d.role == role)
        if not (lo <= n <= hi):
            want = f"exactly {lo}" if lo == hi else f"between {lo} and {hi}"
            raise TopologyError(
                f"{p}: {n} device(s) declared with role {role!r}; this framework supports {want}")


def _check_distinct_serials(p, devices: dict) -> None:
    seen = {}
    for d in devices.values():
        if not d.expect_serial:
            continue
        if d.expect_serial in seen:
            raise TopologyError(
                f"{p}: {d.name!r} and {seen[d.expect_serial]!r} declare the same serial "
                f"{d.expect_serial} — they are one radio, and it cannot hold two roles")
        seen[d.expect_serial] = d.name


# ---- tier A: cross-check the declaration against the hardware -------------


def verify_identity(topo: Topology, manifests: dict) -> list:
    """Compare the declaration with live manifests, keyed by role (``{"transmitter": m, ...}``).

    Returns the disagreements. An empty list means every declared fact about every device was
    confirmed against the hardware -- which is the property that would have caught mock manifests
    being served while real radios transmitted.
    """
    findings = []
    for dev in topo.devices.values():
        m = manifests.get(dev.role)
        if m is None:
            if dev.role != "jammer":       # the jammer is checked by its own instrument
                findings.append(Finding(dev.name, "presence", "declared", "not detected"))
            continue
        if dev.expect_serial and m.serial != dev.expect_serial:
            findings.append(Finding(dev.name, "serial", dev.expect_serial, m.serial))
        chip = (m.clock or {}).get("chip_model")
        if dev.expect_chip and chip and chip != dev.expect_chip:
            findings.append(Finding(dev.name, "chip", dev.expect_chip, chip))
        if dev.tuning_range_hz and m.tuning_range_hz:
            want = [float(x) for x in dev.tuning_range_hz]
            got = [float(x) for x in m.tuning_range_hz]
            if want != got:
                findings.append(Finding(dev.name, "tuning_range_hz", want, got))
    return findings
