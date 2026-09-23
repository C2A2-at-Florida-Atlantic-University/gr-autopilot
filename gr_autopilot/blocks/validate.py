"""Connection validation from port signatures (spec §5.1).

Resolves templated port dtypes (``${type}``, ``${type.attr}``) against concrete parameter
values, then checks domain / dtype / vector-length compatibility. Returns a structured
result rather than raising, so the caller can surface a specific fix (as an MCP tool would).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from gr_autopilot.blocks.registry import BlockDef, PortSig

_TEMPLATE = re.compile(r"^\$\{\s*([^}]+?)\s*\}$")

# Canonical stream item types and a few common synonyms.
_DTYPE_ALIASES = {
    "complex": "complex", "gr_complex": "complex", "fc32": "complex", "complex float": "complex",
    "float": "float", "real": "float", "f32": "float",
    "int": "int", "int32": "int",
    "short": "short", "int16": "short",
    "byte": "byte", "char": "byte", "uint8": "byte", "int8": "byte",
    "bit": "byte", "bits": "byte",
}


def _canon(dtype: str) -> str:
    return _DTYPE_ALIASES.get(str(dtype).strip().lower(), str(dtype).strip().lower())


def resolve_port_dtype(port: PortSig, block: BlockDef, param_values: dict | None = None) -> str:
    """Resolve a possibly-templated port dtype to a canonical stream type.

    Returns the canonical dtype (e.g. ``"complex"``) or ``"unknown"`` if it cannot be
    resolved from the given parameter values.
    """
    param_values = dict(param_values or {})
    raw = str(port.dtype).strip()
    if not raw:
        return "unknown"

    m = _TEMPLATE.match(raw)
    if not m:
        return _canon(raw)

    expr = m.group(1).strip()
    # Simple forms: ${param} or ${param.attr}. Anything more complex -> unknown.
    if "." in expr:
        pname, attr = expr.split(".", 1)
        pname, attr = pname.strip(), attr.strip()
        pdef = block.params.get(pname)
        if pdef is None:
            return "unknown"
        value = param_values.get(pname, pdef.default)
        if value is None or not pdef.options or attr not in pdef.option_attributes:
            return "unknown"
        try:
            idx = [str(o) for o in pdef.options].index(str(value))
        except ValueError:
            return "unknown"
        attrs = pdef.option_attributes[attr]
        if idx < len(attrs):
            return _canon(attrs[idx])
        return "unknown"

    pdef = block.params.get(expr)
    if pdef is None:
        return "unknown"
    value = param_values.get(expr, pdef.default)
    return _canon(value) if value is not None else "unknown"


@dataclass
class ConnCheck:
    ok: bool | None          # True compatible, False incompatible, None indeterminate
    reason: str
    src_dtype: str = ""
    dst_dtype: str = ""


def check_connection(
    src_block: BlockDef,
    src_port_idx: int,
    dst_block: BlockDef,
    dst_port_idx: int,
    src_params: dict | None = None,
    dst_params: dict | None = None,
) -> ConnCheck:
    """Validate connecting ``src_block.outputs[i] -> dst_block.inputs[j]``."""
    try:
        src_port = src_block.outputs[src_port_idx]
    except IndexError:
        return ConnCheck(False, f"{src_block.id} has no output port {src_port_idx}")
    try:
        dst_port = dst_block.inputs[dst_port_idx]
    except IndexError:
        return ConnCheck(False, f"{dst_block.id} has no input port {dst_port_idx}")

    if src_port.domain != dst_port.domain:
        return ConnCheck(
            False,
            f"domain mismatch: {src_block.id}.{src_port.domain} -> "
            f"{dst_block.id}.{dst_port.domain} (stream cannot connect to message)",
        )

    if src_port.domain == "message":
        return ConnCheck(True, "message ports compatible")

    s = resolve_port_dtype(src_port, src_block, src_params)
    d = resolve_port_dtype(dst_port, dst_block, dst_params)
    if s == "unknown" or d == "unknown":
        return ConnCheck(None, f"dtype indeterminate ({s} -> {d}); set the type parameter",
                         src_dtype=s, dst_dtype=d)
    if s != d:
        return ConnCheck(
            False,
            f"item-type mismatch: {src_block.id} emits {s} but {dst_block.id} expects {d} "
            f"(insert a converter block)",
            src_dtype=s, dst_dtype=d,
        )
    return ConnCheck(True, f"{s} -> {d} compatible", src_dtype=s, dst_dtype=d)
