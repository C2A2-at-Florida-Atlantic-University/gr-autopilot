"""FlowgraphSpec — the whole-graph-as-JSON construction surface (spec §8, revised).

The agent submits an entire transceiver as one object: the structural choices (modulation,
coding, sync) plus the TX/RX skill chains and pulse-shape parameters. Edits between
iterations are expressed by submitting a modified spec; there is no stateful builder. The
spec doubles as the edit-ledger's per-iteration structure artifact.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FlowgraphSpec:
    structure_id: str
    modulation: str                       # bpsk | qpsk | 16qam (structural)
    tx_chain: list[str]                   # ordered skill names, bits -> complex
    rx_chain: list[str]                   # ordered skill names, complex -> bits
    pulse_shape: dict = field(default_factory=lambda: {"type": "rrc", "sps": 4, "rolloff": 0.35})
    sync: dict = field(default_factory=lambda: {"timing": "data_aided"})
    coding: Any = None                    # Stage 1: None (structural, future)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), **kw)

    @classmethod
    def from_dict(cls, d: dict) -> "FlowgraphSpec":
        return cls(
            structure_id=d["structure_id"],
            modulation=d["modulation"],
            tx_chain=list(d["tx_chain"]),
            rx_chain=list(d["rx_chain"]),
            pulse_shape=dict(d.get("pulse_shape", {"type": "rrc", "sps": 4, "rolloff": 0.35})),
            sync=dict(d.get("sync", {"timing": "data_aided"})),
            coding=d.get("coding"),
        )

    @classmethod
    def from_json(cls, s: str) -> "FlowgraphSpec":
        return cls.from_dict(json.loads(s))

    @classmethod
    def link(cls, modulation: str, sps: int = 4, rolloff: float = 0.35,
             sync: str = "data_aided", structure_id: str | None = None,
             coding: str | None = None) -> "FlowgraphSpec":
        """Canonical single-carrier transceiver spec for a given modulation (+ optional FEC)."""
        from gr_autopilot.modulation import canonical
        mod = canonical(modulation)
        sid = structure_id or (f"{mod}_link" if coding is None else f"{mod}_{coding}_link")
        return cls(
            structure_id=sid,
            modulation=mod,
            tx_chain=[f"{_short(mod)}_mod", "rrc_pulse_shape"],
            rx_chain=["rrc_matched_filter", "symbol_sync", f"{_short(mod)}_demod"],
            pulse_shape={"type": "rrc", "sps": sps, "rolloff": rolloff},
            sync={"timing": sync},
            coding=coding,
        )


_SHORT = {"bpsk": "bpsk", "qpsk": "qpsk", "8psk": "psk8", "16qam": "qam16",
          "32qam": "qam32", "64qam": "qam64", "256qam": "qam256"}


def _short(mod: str) -> str:
    """The skill-name stem for a modulation (skill names cannot start with a digit)."""
    try:
        return _SHORT[mod]
    except KeyError:
        raise ValueError(f"unknown modulation {mod!r} (use one of {', '.join(_SHORT)})") from None
