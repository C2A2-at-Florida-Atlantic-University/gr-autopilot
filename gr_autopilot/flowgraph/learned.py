"""Learned skills — the agent's self-authored, test-gated vocabulary growth (spec C5).

A *learned skill* is a named, benchmarked transceiver recipe (a ``FlowgraphSpec`` the agent
discovered works, plus the measurement that justifies it and the baseline it beat). The agent
proposes one after experimenting on the framework grader; promotion is **test-gated** (it must
re-validate and beat its baseline by a margin) and **append-only persisted**, so the vocabulary the
agent composes with grows across runs — self-improvement reframed as vocabulary growth, without the
agent ever writing (or grading) its own scoring path.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class LearnedSkill:
    name: str
    spec: dict                       # a FlowgraphSpec.to_dict() the agent promotes as reusable
    objective: str                   # what it optimizes, e.g. "max spectral efficiency meeting BER<=1e-2"
    benchmark: dict = field(default_factory=dict)   # measured justification (efficiency, ber, gain, ...)
    parent: str = ""                 # the baseline modcod/skill it improved on
    kind: str = "structure"          # a composite/structure-level learned skill

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LearnedSkill":
        return cls(name=d["name"], spec=dict(d["spec"]), objective=d.get("objective", ""),
                   benchmark=dict(d.get("benchmark", {})), parent=d.get("parent", ""),
                   kind=d.get("kind", "structure"))


class LearnedSkillStore:
    """In-memory registry of learned skills, backed by an append-only JSONL file so they persist."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._skills: dict[str, LearnedSkill] = {}
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:                          # tolerate a truncated final line from a crash mid-append,
                d = json.loads(line)      # exactly as telemetry.server._read_ledger does — one bad
            except json.JSONDecodeError:  # line must not brick the whole learned vocabulary on load
                continue
            s = LearnedSkill.from_dict(d)
            self._skills[s.name] = s

    def add(self, skill: LearnedSkill) -> None:
        self._skills[skill.name] = skill
        if self.path is not None:                    # append-only persistence (ledger-style)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(skill.to_dict()) + "\n")

    def list(self) -> list[dict]:
        return [{"name": s.name, "kind": s.kind, "objective": s.objective,
                 "benchmark": s.benchmark, "parent": s.parent} for s in self._skills.values()]

    def get(self, name: str) -> LearnedSkill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __len__(self) -> int:
        return len(self._skills)
