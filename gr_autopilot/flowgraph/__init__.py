"""Flowgraph construction (spec §5.2, §8, M2).

The agent authors a whole flowgraph as one JSON ``FlowgraphSpec`` (the whole-graph-as-JSON
construction surface — see spec §8 revised note), composing curated *skills*. The builder
validates the spec as a unit (skill existence + port-dtype chaining + parameter ranges) and
compiles it to a runnable link realized by a ``LinkBackend`` (sim now, Pluto later).
"""
from gr_autopilot.flowgraph.builder import ValidationResult, build_and_run, compile_spec, validate_spec
from gr_autopilot.flowgraph.skills_registry import Skill, SkillsRegistry, default_skills
from gr_autopilot.flowgraph.spec import FlowgraphSpec

__all__ = [
    "FlowgraphSpec",
    "Skill",
    "SkillsRegistry",
    "default_skills",
    "ValidationResult",
    "validate_spec",
    "compile_spec",
    "build_and_run",
]
