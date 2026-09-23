"""Validate a whole FlowgraphSpec, then compile + run it via a LinkBackend (spec §8, M2).

Validation is a single whole-graph pass (spec §8 revised): it checks skill existence,
port-dtype chaining through each chain, modulator/demod agreement with the declared
modulation, and parameter ranges — surfacing every problem at once as an actionable list.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gr_autopilot.flowgraph.skills_registry import SkillsRegistry, default_skills
from gr_autopilot.flowgraph.spec import FlowgraphSpec
from gr_autopilot.link.backend import LinkBackend, LinkParams, LinkResult
from gr_autopilot.modulation import MODULATIONS


@dataclass
class ValidationResult:
    ok: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _validate_chain(chain, skills, expect_first_in, expect_last_out, label, errors):
    """Walk a skill chain, checking existence and port-dtype continuity."""
    if not chain:
        errors.append(f"{label} is empty")
        return
    cur = expect_first_in
    for i, name in enumerate(chain):
        sk = skills.get(name)
        if sk is None:
            errors.append(f"{label}[{i}]: unknown skill {name!r}")
            return
        if sk.framework_owned:
            errors.append(f"{label}[{i}]: {name!r} is framework-owned and cannot be placed by the agent")
        if sk.in_dtype != cur:
            errors.append(
                f"{label}[{i}]: {name!r} consumes {sk.in_dtype!r} but the previous stage "
                f"produces {cur!r} (item-type mismatch)")
        cur = sk.out_dtype
    if cur != expect_last_out:
        errors.append(f"{label} must end producing {expect_last_out!r}, but ends with {cur!r}")


def validate_spec(spec: FlowgraphSpec, skills: SkillsRegistry | None = None) -> ValidationResult:
    skills = skills or default_skills()
    errors: list[str] = []
    warnings: list[str] = []

    if spec.modulation not in MODULATIONS:
        errors.append(f"unknown modulation {spec.modulation!r} (choose from {list(MODULATIONS)})")

    _validate_chain(spec.tx_chain, skills, "bits", "complex", "tx_chain", errors)
    _validate_chain(spec.rx_chain, skills, "complex", "bits", "rx_chain", errors)

    # Modulator / demod must agree with the declared modulation.
    mods = [skills.get(n) for n in spec.tx_chain if skills.get(n) and skills.get(n).kind == "modulator"]
    if not mods:
        errors.append("tx_chain has no modulator skill")
    elif mods[0].modulation != spec.modulation:
        errors.append(f"tx modulator {mods[0].name!r} is {mods[0].modulation}, "
                      f"but spec.modulation is {spec.modulation}")
    demods = [skills.get(n) for n in spec.rx_chain if skills.get(n) and skills.get(n).kind == "demod"]
    if not demods:
        errors.append("rx_chain has no demodulator skill")
    elif demods[-1].modulation != spec.modulation:
        errors.append(f"rx demodulator {demods[-1].name!r} is {demods[-1].modulation}, "
                      f"but spec.modulation is {spec.modulation}")

    # Pulse-shape parameter ranges.
    ps = spec.pulse_shape or {}
    sps = ps.get("sps", 4)
    rolloff = ps.get("rolloff", 0.35)
    if not (isinstance(sps, int) and 1 <= sps <= 16):
        errors.append(f"pulse_shape.sps must be an int in [1,16], got {sps!r}")
    if not (isinstance(rolloff, (int, float)) and 0.0 <= rolloff <= 1.0):
        errors.append(f"pulse_shape.rolloff must be in [0.0,1.0], got {rolloff!r}")

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)


def compile_spec(spec: FlowgraphSpec) -> dict:
    """Reduce a validated spec to the link parameters the backend realizes.

    The chains are carried through rather than dropped. Backends with a fixed internal pipeline
    ignore them; :class:`~gr_autopilot.link.gr_spec.GRSpecBackend` instantiates one real GNU Radio
    block per entry, which is what makes the agent's structural choices measurable rather than
    decorative.
    """
    ps = spec.pulse_shape or {}
    sync = spec.sync or {}
    out = {
        "modulation": spec.modulation,
        "sps": int(ps.get("sps", 4)),
        "rolloff": float(ps.get("rolloff", 0.35)),
        "coding": spec.coding,
        "tx_chain": tuple(spec.tx_chain or ()),
        "rx_chain": tuple(spec.rx_chain or ()),
    }
    # ``sync`` had no consumer at all until now. It carries the synchronizer's loop bandwidth,
    # which is what makes that setting editable in an exported flowgraph: change it in the
    # editor, hand the file back, and the measurement changes.
    if sync.get("loop_bw") is not None:
        try:
            out["timing_loop_bw"] = float(sync["loop_bw"])
        except (TypeError, ValueError):
            pass
    return out


def build_and_run(spec: FlowgraphSpec, backend: LinkBackend,
                  skills: SkillsRegistry | None = None, **condition) -> LinkResult:
    """Validate the spec, compile it, and run one trial on ``backend``.

    ``condition`` carries the channel/operator-set parameters (es_n0_db or noise_voltage,
    n_payload_bits, seed, ...) that are NOT part of the agent's structural choice.
    """
    result = validate_spec(spec, skills)
    if not result.ok:
        raise ValueError("invalid flowgraph spec:\n  - " + "\n  - ".join(result.errors))
    params = LinkParams(**{**compile_spec(spec), **condition})
    return backend.run_link(params)
