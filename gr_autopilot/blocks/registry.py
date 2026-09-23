"""Parse installed GRC block descriptors into a queryable registry."""
from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Default location(s) of installed GRC block YAML descriptors.
_DEFAULT_DIRS = [
    "/usr/share/gnuradio/grc/blocks",
    # Out-of-tree blocks install here. Scanning only the distribution directory made the catalogue
    # disagree with the compiler in BOTH directions: six OOT blocks the compiler accepts were
    # invisible to anyone browsing (so "GNU Radio is the platform" quietly excluded a user's own
    # work), and the five blocks the framework refuses were invisible too, which is why the
    # deny-list in flowgraph/platform.py is keyed on identity rather than on this catalogue.
    "/usr/local/share/gnuradio/grc/blocks",
]

#: Honoured like GNU Radio's own tooling does, so a bench with blocks elsewhere is describable
#: without editing this file.
_ENV_DIRS = [d for d in os.environ.get("GRC_BLOCKS_PATH", "").split(os.pathsep) if d]


@dataclass
class ParamDef:
    id: str
    label: str = ""
    dtype: str = ""
    default: Any = None
    options: list = field(default_factory=list)
    option_attributes: dict = field(default_factory=dict)


@dataclass
class PortSig:
    """A block port signature (spec §5.1)."""

    domain: str = "stream"      # stream | message
    dtype: str = ""             # literal ('complex','float',...) or template ('${type}')
    vlen: Any = 1               # literal int or template
    optional: bool = False
    id: str = ""


@dataclass
class BlockDef:
    id: str
    label: str = ""
    category: str = ""
    flags: list = field(default_factory=list)
    params: dict[str, ParamDef] = field(default_factory=dict)
    inputs: list[PortSig] = field(default_factory=list)
    outputs: list[PortSig] = field(default_factory=list)

    def summary(self) -> dict:
        return {"id": self.id, "label": self.label, "category": self.category}

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "flags": self.flags,
            "parameters": [
                {"id": p.id, "label": p.label, "dtype": p.dtype, "default": p.default,
                 "options": p.options}
                for p in self.params.values()
            ],
            "inputs": [_port_dict(p) for p in self.inputs],
            "outputs": [_port_dict(p) for p in self.outputs],
        }


def _port_dict(p: PortSig) -> dict:
    return {"id": p.id, "domain": p.domain, "dtype": p.dtype, "vlen": p.vlen,
            "optional": p.optional}


def _as_port(raw: dict) -> PortSig:
    return PortSig(
        domain=str(raw.get("domain", "stream")),
        dtype=str(raw.get("dtype", "")) if raw.get("dtype") is not None else "",
        vlen=raw.get("vlen", 1),
        optional=bool(raw.get("optional", False)),
        id=str(raw.get("id", "")),
    )


def _parse_block(path: str) -> BlockDef | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:  # a malformed block file must not vanish silently —
        log.warning("skipping unparseable block file %s: %s", path, exc)  # name it so it's fixable
        return None
    if not isinstance(doc, dict) or "id" not in doc:
        log.warning("skipping block file %s: not a mapping with an 'id'", path)
        return None

    params: dict[str, ParamDef] = {}
    for p in doc.get("parameters", []) or []:
        if not isinstance(p, dict) or "id" not in p:
            continue
        params[p["id"]] = ParamDef(
            id=p["id"],
            label=str(p.get("label", "")),
            dtype=str(p.get("dtype", "")),
            default=p.get("default"),
            options=list(p.get("options", []) or []),
            option_attributes=dict(p.get("option_attributes", {}) or {}),
        )

    category = doc.get("category", "")
    if isinstance(category, list):
        category = "/".join(str(c) for c in category)

    return BlockDef(
        id=str(doc["id"]),
        label=str(doc.get("label", doc["id"])),
        category=str(category),
        flags=list(doc.get("flags", []) or []),
        params=params,
        inputs=[_as_port(p) for p in (doc.get("inputs", []) or []) if isinstance(p, dict)],
        outputs=[_as_port(p) for p in (doc.get("outputs", []) or []) if isinstance(p, dict)],
    )


class BlockRegistry:
    """Discovered inventory of installed GNU Radio blocks (spec §5.1)."""

    def __init__(self, block_dirs: list[str] | None = None):
        self.block_dirs = block_dirs or [
            d for d in (_ENV_DIRS + _DEFAULT_DIRS) if os.path.isdir(d)]
        self._blocks: dict[str, BlockDef] = {}
        self._load()

    def _load(self) -> None:
        for d in self.block_dirs:
            for path in sorted(glob.glob(os.path.join(d, "*.block.yml"))):
                blk = _parse_block(path)
                if blk is not None:
                    self._blocks[blk.id] = blk

    # ---- discovery API (MCP tools) ----------------------------------------

    def list_blocks(self, category: str | None = None, search: str | None = None) -> list[dict]:
        out = []
        for blk in self._blocks.values():
            if category and category.lower() not in blk.category.lower():
                continue
            if search:
                s = search.lower()
                if s not in blk.id.lower() and s not in blk.label.lower():
                    continue
            out.append(blk.summary())
        return sorted(out, key=lambda b: b["id"])

    def describe_block(self, name: str) -> dict:
        blk = self._blocks.get(name)
        if blk is None:
            raise KeyError(f"unknown block {name!r}")
        return blk.describe()

    def get(self, name: str) -> BlockDef | None:
        return self._blocks.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._blocks

    def __len__(self) -> int:
        return len(self._blocks)


@lru_cache(maxsize=1)
def default_registry() -> BlockRegistry:
    """A process-wide cached registry (parsing ~600 files once)."""
    return BlockRegistry()
