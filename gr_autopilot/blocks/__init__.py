"""Block registry (spec §5.1): dynamic discovery of installed GNU Radio blocks.

Introspects the same ``grc/*.block.yml`` descriptors GRC uses, so the agent's DSP
vocabulary is discovered at runtime rather than hard-coded. Port-signature data enables
connection validation before runtime (spec §5.1) — a complex-vs-float mismatch fails with a
structured error instead of a cryptic runtime abort.
"""
from gr_autopilot.blocks.registry import BlockDef, BlockRegistry, ParamDef, PortSig
from gr_autopilot.blocks.validate import ConnCheck, check_connection, resolve_port_dtype

__all__ = [
    "BlockDef",
    "BlockRegistry",
    "ParamDef",
    "PortSig",
    "ConnCheck",
    "check_connection",
    "resolve_port_dtype",
]
