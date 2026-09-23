"""Skills registry (spec §5.2): curated, contract-bearing composite subgraphs.

A skill is a parameterized subgraph with a port contract. The agent composes skills when
they fit and drops to raw blocks (§5.1) when they don't — both layers, agent chooses
granularity. Stage 1 realizes skills through the link backend (a hier-block implementation
in ``skills/`` can register here later without changing the contract).

Port dtypes use two logical types on the link: ``bits`` and ``complex`` (baseband IQ).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Skill:
    name: str
    kind: str                # modulator | pulse_shape | matched_filter | sync | demod | channel
    in_dtype: str            # 'bits' | 'complex'
    out_dtype: str
    params: dict = field(default_factory=dict)   # name -> {default, bounds?, type}
    modulation: str | None = None                 # set for modulator/demod skills
    framework_owned: bool = False                 # e.g. the channel/grader path
    doc: str = ""

    def describe(self) -> dict:
        return {
            "name": self.name, "kind": self.kind,
            "in_dtype": self.in_dtype, "out_dtype": self.out_dtype,
            "params": self.params, "modulation": self.modulation,
            "framework_owned": self.framework_owned, "doc": self.doc,
        }


_PULSE_PARAMS = {
    "sps": {"type": "int", "default": 4, "bounds": [1, 16]},
    "rolloff": {"type": "float", "default": 0.35, "bounds": [0.0, 1.0]},
}

_STAGE1_SKILLS = [
    # Modulators: bits -> complex symbols.
    Skill("bpsk_mod", "modulator", "bits", "complex", modulation="bpsk", doc="BPSK constellation mapper"),
    Skill("qpsk_mod", "modulator", "bits", "complex", modulation="qpsk", doc="QPSK (Gray) constellation mapper"),
    Skill("psk8_mod", "modulator", "bits", "complex", modulation="8psk", doc="8-PSK (Gray) constellation mapper, 3 bits/symbol, constant modulus"),
    Skill("qam16_mod", "modulator", "bits", "complex", modulation="16qam", doc="16-QAM (Gray) constellation mapper"),
    Skill("qam32_mod", "modulator", "bits", "complex", modulation="32qam", doc="32-QAM cross constellation mapper, 5 bits/symbol"),
    Skill("qam64_mod", "modulator", "bits", "complex", modulation="64qam", doc="64-QAM (Gray) constellation mapper, 6 bits/symbol"),
    Skill("qam256_mod", "modulator", "bits", "complex", modulation="256qam", doc="256-QAM (Gray) constellation mapper, 8 bits/symbol -- simulation; beyond the radios' error-vector floor"),
    # Pulse shaping / matched filtering.
    Skill("rrc_pulse_shape", "pulse_shape", "complex", "complex", params=dict(_PULSE_PARAMS),
          doc="root-raised-cosine transmit pulse shaping"),
    Skill("rrc_matched_filter", "matched_filter", "complex", "complex", params=dict(_PULSE_PARAMS),
          doc="root-raised-cosine matched receive filter"),
    # Synchronization.
    Skill("symbol_sync", "sync", "complex", "complex",
          params={"scheme": {"type": "enum", "default": "data_aided",
                             "options": ["data_aided", "gardner"]}},
          doc="symbol timing recovery: finds the instant within each symbol at which to sample, "
              "and outputs one sample per symbol. Without it the receiver has no symbol grid."),
    Skill("costas_carrier", "sync", "complex", "complex",
          params={"loop_bw": {"type": "float", "default": 0.0628, "bounds": [0.001, 0.2]}},
          doc="carrier phase and frequency recovery (Costas loop). Needed when the transmitter "
              "and receiver oscillators differ, which rotates the constellation. Requires a "
              "constant-modulus constellation: BPSK, QPSK or 8-PSK, not QAM."),
    Skill("agc", "agc", "complex", "complex",
          doc="automatic gain control: normalizes signal amplitude. Matters for constellations "
              "that carry information in amplitude: every QAM."),
    # Demodulators: complex symbols -> bits.
    Skill("bpsk_demod", "demod", "complex", "bits", modulation="bpsk", doc="BPSK hard decision"),
    Skill("qpsk_demod", "demod", "complex", "bits", modulation="qpsk", doc="QPSK hard decision"),
    Skill("psk8_demod", "demod", "complex", "bits", modulation="8psk", doc="8-PSK hard decision"),
    Skill("qam16_demod", "demod", "complex", "bits", modulation="16qam", doc="16-QAM hard decision"),
    Skill("qam32_demod", "demod", "complex", "bits", modulation="32qam", doc="32-QAM hard decision"),
    Skill("qam64_demod", "demod", "complex", "bits", modulation="64qam", doc="64-QAM hard decision"),
    Skill("qam256_demod", "demod", "complex", "bits", modulation="256qam", doc="256-QAM hard decision"),
    # Framework-owned channel (agent does not place it; the framework inserts it between
    # the TX and RX chains — sim channel now, the cabled RF path + SDRs later).
    Skill("awgn_channel", "channel", "complex", "complex", framework_owned=True,
          doc="framework-owned channel (sim AWGN / real RF path)"),
]


class SkillsRegistry:
    """Curated composite subgraphs (spec §5.2)."""

    def __init__(self, skills: list[Skill] | None = None):
        self._skills: dict[str, Skill] = {}
        for s in (skills if skills is not None else _STAGE1_SKILLS):
            self._skills[s.name] = s

    def list_skills(self) -> list[dict]:
        return [{"name": s.name, "kind": s.kind, "modulation": s.modulation,
                 "framework_owned": s.framework_owned} for s in self._skills.values()]

    def describe_skill(self, name: str) -> dict:
        if name not in self._skills:
            raise KeyError(f"unknown skill {name!r}")
        return self._skills[name].describe()

    def add_skill(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def modulator_for(self, modulation: str) -> Skill | None:
        for s in self._skills.values():
            if s.kind == "modulator" and s.modulation == modulation:
                return s
        return None


def default_skills() -> SkillsRegistry:
    return SkillsRegistry()
