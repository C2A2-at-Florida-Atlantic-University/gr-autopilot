"""The Companion document, compiled and executed by GNU Radio.

This is what "flowgraph-first" means concretely: the agent authors a ``.grc``, GNU Radio's own
compiler turns it into a ``top_block``, and that object is what runs. There is no second
description of the graph, so the file an operator opens in Companion is the file that produced the
measurement -- not a rendering of some other structure that happened to run.

That distinction is the whole point. Before this, the hardware path executed a fixed hand-written
pipeline and ``export_flowgraph`` wrote a ``.grc`` derived from a *spec*, so the exported file
depicted a graph that had never run. It was openable, plausible, and wrong.

HOW IT WORKS
------------
``platform.load_and_generate_flow_graph`` writes a Python module holding ``class <id>(gr.top_block)``
-- the same generator the Companion "Generate" button calls. We import that module and instantiate
the class. Verified end to end on this bench: a three-block document compiled and ran, delivering
4096 samples.

Two details that matter and are easy to get wrong:

* **The class name is the document's ``options.id``**, so the id has to be a valid Python
  identifier. A document whose id collides with another in the same interpreter would shadow it in
  ``sys.modules``; the generated module is therefore registered under a name derived from the
  document's content hash, not its id.
* **Generation is cached on the content hash.** Compiling costs ~0.07 s, which is negligible once
  but not when a search re-runs the same structure across dozens of trials. The hash is also the
  identity used to name the stored artifact, so an iteration that did not change the graph does not
  produce a second copy of it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import threading
from pathlib import Path

import yaml

from gr_autopilot.flowgraph.platform import DocumentRejected, check_document, platform

#: Compiled documents, keyed by content hash -> generated top_block class.
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def document_id(doc: dict) -> str:
    """The ``options.id``, which GRC uses as the generated class name."""
    return str(((doc.get("options") or {}).get("parameters") or {}).get("id") or "").strip()


def document_hash(doc: dict) -> str:
    """Content identity of a document.

    Computed on the canonical YAML dump rather than the file bytes, so a document that only differs
    in key order or block coordinates -- neither of which changes what runs -- hashes the same and
    is not stored twice.
    """
    canonical = _strip_layout(doc)
    return hashlib.sha256(
        yaml.safe_dump(canonical, sort_keys=True).encode("utf-8")).hexdigest()


def _strip_layout(doc: dict) -> dict:
    """Drop everything that affects only the picture, not the signal."""
    out = {k: v for k, v in doc.items() if k != "metadata"}
    blocks = []
    for b in out.get("blocks") or []:
        blocks.append({k: v for k, v in b.items() if k != "states"})
    if blocks:
        out["blocks"] = blocks
    if "options" in out and isinstance(out["options"], dict):
        out["options"] = {k: v for k, v in out["options"].items() if k != "states"}
    return out


def dump(doc: dict) -> str:
    """Serialise a document the way Companion writes them."""
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def write(doc: dict, path) -> Path:
    """Write a document to disk as a ``.grc`` an operator can open in Companion."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump(doc), encoding="utf-8")
    return p


def load(path) -> dict:
    """Read a ``.grc`` from disk, refusing anything the gate rejects."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise DocumentRejected([f"not readable as a Companion file: {exc}"]) from exc
    check_document(doc)
    return doc


def compile_document(doc: dict, workdir):
    """Compile a document into a ``top_block`` CLASS, ready to instantiate.

    Returns the class, not an instance: the caller decides when to construct, which matters on
    hardware where construction opens the radios.
    """
    ident = document_id(doc)
    if not _IDENT.match(ident or ""):
        raise DocumentRejected([
            f"options.id is {ident!r}; it becomes the generated Python class name, so it must be a "
            f"valid identifier (letters, digits and underscore, not starting with a digit)"])

    key = document_hash(doc)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    check_document(doc)
    out = Path(workdir) / key[:12]
    out.mkdir(parents=True, exist_ok=True)
    grc_path = out / f"{ident}.grc"
    write(doc, grc_path)

    try:
        platform().load_and_generate_flow_graph(str(grc_path), str(out))
    except Exception as exc:  # noqa: BLE001 - GRC raises several types; all mean "will not compile"
        raise DocumentRejected([f"GNU Radio could not generate this graph: {exc}"]) from exc

    py = out / f"{ident}.py"
    if not py.exists():
        found = sorted(p.name for p in out.glob("*.py"))
        raise DocumentRejected([
            f"GNU Radio generated no module for {ident!r}" +
            (f" (found {', '.join(found)})" if found else "")])

    # Register under the content hash: two documents may legitimately share an options.id, and
    # importing both under that name would silently give the second the first's graph.
    modname = f"gr_autopilot_fg_{key[:16]}"
    spec = importlib.util.spec_from_file_location(modname, py)
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        del sys.modules[modname]
        raise DocumentRejected([f"the generated graph does not import: {exc}"]) from exc

    cls = getattr(module, ident, None)
    if cls is None:
        raise DocumentRejected([
            f"the generated module defines no class {ident!r} "
            f"(found {', '.join(n for n in vars(module) if not n.startswith('_'))[:200]})"])

    with _CACHE_LOCK:
        _CACHE[key] = cls
    return cls


def block_names(doc: dict) -> list:
    """The block instance names in document order — what the dashboard draws and what an
    attestation pass walks."""
    return [b.get("name") for b in (doc.get("blocks") or []) if isinstance(b, dict)]


def connections(doc: dict) -> list:
    """Declared edges as ``(src, src_port, dst, dst_port)`` tuples.

    Ports are kept as STRINGS. Stream ports are numeric, but message ports are named, and coercing
    them to integers is what makes a renderer or a graph walk break on the first packet-radio
    graph.
    """
    out = []
    for c in doc.get("connections") or []:
        if isinstance(c, (list, tuple)) and len(c) == 4:
            out.append((str(c[0]), str(c[1]), str(c[2]), str(c[3])))
    return out
