"""GNU Radio Companion as the compiler, and the gate in front of it.

The agent authors a Companion document and GNU Radio compiles it. We do not write a graph
compiler: ``gnuradio.grc.core.platform.Platform`` is installed on the bench, loads 608 block
classes in 0.13 s, type-checks connections, evaluates parameter expressions and generates a
runnable ``top_block`` -- all of it maintained by the GNU Radio project and identical to what the
Companion editor does. A hand-written equivalent would be a second, worse implementation of a
moving target, and the project has a standing rule against that.

What we DO have to write is the gate, because "let the model author a Companion document" is
"let the model run code" unless five specific blocks are refused.

THE GAP THAT MAKES THIS URGENT
------------------------------
The curated catalogue in :mod:`gr_autopilot.blocks.registry` parses 597 ``.block.yml`` descriptors.
The platform loads 608 classes. Measured on this bench, the twelve-block difference is::

    epy_block  epy_module  import  virtual_sink  virtual_source  _dummy
    NTSC_decoder_c  NTSC_transmitter_c  NTSC_video_stream_converter_c
    mobrffi_get_fingerprint  spectrumDetect_specDetect  spectrumDetect_spectrumPlot

Every block that breaks the sandbox is in the set the catalogue cannot see. An agent browsing
``list_blocks`` would never find them; a document naming one compiles anyway. So the deny-list is
keyed on block IDENTITY and applied to the document, never inferred from what the catalogue happens
to contain, and never from port arity -- ``epy_block`` declares stream ports precisely so it can sit
mid-chain, and would pass any arity test.

WHY EACH ONE IS REFUSED
-----------------------
``epy_block`` / ``epy_module`` carry arbitrary Python in a ``_source_code`` parameter: the document
becomes a program. ``import`` injects an arbitrary import into the generated module, which is the
same hole wearing a different hat.

``virtual_source`` / ``virtual_sink`` are subtler and matter more. They connect by *name*, so a
virtual pair forms an edge that never appears in the document's ``connections`` list. Any integrity
check that walks declared edges -- which is how we prove the graded signal actually crossed the RF
path -- would see an unconnected fragment and pass a graph whose modulator feeds its own receiver.
That is bit error ratio zero with no channel and no radio: the exact result the measurement exists
to make impossible, obtained without touching the grader.

The six out-of-tree blocks are legitimate and are made visible to the catalogue instead
(``blocks/registry.py`` now also scans ``/usr/local/share/gnuradio/grc/blocks``), because "GNU Radio
is the platform" has to include a user's own blocks.
"""
from __future__ import annotations

import threading
from pathlib import Path

#: Refused by identity, always, in every mode. Not a policy knob: each of these makes the document
#: something other than a signal-processing graph, and the first three make it arbitrary code.
DENIED_BLOCKS = frozenset({
    "epy_block",        # arbitrary Python in a parameter; declares stream ports, so arity cannot catch it
    "epy_module",       # ditto, as an importable module
    "import",           # arbitrary import injected into the generated program
    "virtual_source",   # forms an edge absent from `connections` -- defeats edge-walking attestation
    "virtual_sink",     # ditto
    "_dummy",           # GRC's placeholder for an unresolved block; never a real one
})

#: Refused because the daemon runs headless: a Qt block compiles and then blocks or crashes at run
#: time, which presents as a hung experiment rather than as a rejected document.
DENIED_PREFIXES = ("qtgui_", "fosphor_", "video_sdl_")

#: The generated program must be a plain ``top_block``. A document asking for a Qt or WX top block
#: generates code that cannot run without a display, so the option is pinned rather than trusted.
REQUIRED_GENERATE_OPTIONS = "no_gui"


class DocumentRejected(ValueError):
    """The document is refused before it reaches the compiler.

    Carries every reason at once: an agent that has to discover its mistakes one compile at a time
    burns a round trip per problem, and the failures are usually correlated.
    """

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


_LOCK = threading.Lock()
_PLATFORM = None


def platform():
    """The shared GRC platform, built once.

    ``build_library`` costs ~0.13 s and is pure setup, but it is not thread-safe and the daemon
    serves concurrent requests, so it is built under a lock and reused.
    """
    global _PLATFORM
    with _LOCK:
        if _PLATFORM is None:
            from gnuradio import gr
            from gnuradio.grc.core.platform import Platform

            _PLATFORM = Platform(
                name="gr-autopilot",
                prefs=gr.prefs(),
                version=gr.version(),
                version_parts=(gr.major_version(), gr.api_version(), gr.minor_version()),
            )
            _PLATFORM.build_library()
        return _PLATFORM


def known_block_ids() -> set:
    """Every block id the compiler will accept, including out-of-tree ones.

    This is the authority on what exists -- not the curated catalogue, which is a *view* for the
    agent to browse and is allowed to be smaller.
    """
    return set(platform().blocks)


def check_document(doc: dict) -> None:
    """Refuse a Companion document the framework will not compile. Raises :class:`DocumentRejected`.

    Runs BEFORE the platform sees the document, because rejecting a block after GRC has evaluated
    its parameters is too late -- parameter expressions are evaluated during ``rewrite()``.
    """
    reasons = []
    if not isinstance(doc, dict):
        raise DocumentRejected(["not a Companion document: expected a mapping"])

    blocks = doc.get("blocks")
    if not isinstance(blocks, list):
        raise DocumentRejected(["not a Companion document: no 'blocks' list"])

    known = known_block_ids()
    for b in blocks:
        if not isinstance(b, dict):
            reasons.append("a block entry is not a mapping")
            continue
        bid = b.get("id")
        name = b.get("name", "?")
        if bid in DENIED_BLOCKS:
            reasons.append(_why_denied(bid, name))
        elif any(bid.startswith(p) for p in DENIED_PREFIXES if isinstance(bid, str)):
            reasons.append(
                f"block {name!r} is {bid!r}: graphical blocks cannot run on this headless daemon")
        elif bid == "options":
            got = (b.get("parameters") or {}).get("generate_options")
            if got is not None and got != REQUIRED_GENERATE_OPTIONS:
                reasons.append(
                    f"the options block asks for generate_options={got!r}; this daemon runs "
                    f"headless and requires {REQUIRED_GENERATE_OPTIONS!r}")
        elif isinstance(bid, str) and bid not in known:
            reasons.append(
                f"block {name!r} has unknown id {bid!r} — it is not in this installation's "
                f"{len(known)} block classes")

    if reasons:
        raise DocumentRejected(reasons)


def _why_denied(bid: str, name: str) -> str:
    """A refusal an agent can act on, rather than a bare 'denied'."""
    if bid in ("epy_block", "epy_module"):
        return (f"block {name!r} is {bid!r}, which carries arbitrary Python in a parameter. Express "
                f"the operation with signal-processing blocks instead.")
    if bid == "import":
        return (f"block {name!r} is an 'import' block, which injects arbitrary code into the "
                f"generated program.")
    if bid in ("virtual_source", "virtual_sink"):
        return (f"block {name!r} is {bid!r}. Virtual connections do not appear in the document's "
                f"connection list, so the framework cannot verify that the graded signal crossed "
                f"the radio path. Connect the blocks directly.")
    return f"block {name!r} is {bid!r}, which this framework does not accept."


def load_document(path) -> dict:
    """Parse a ``.grc`` into its raw dict, refusing anything the gate rejects."""
    import yaml

    raw = Path(path).read_bytes()
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - yaml raises several types
        raise DocumentRejected([f"not readable as a Companion file: {exc}"]) from exc
    check_document(doc)
    return doc


def validate_document(doc: dict, name: str = "graph"):
    """Gate, then hand the document to GRC for real validation.

    Returns the validated ``FlowGraph``. GRC's own checker is what reports port-type and vlen
    mismatches, unevaluatable parameter expressions and unconnected required ports -- measured on
    this bench, it names both the block and the parameter, which is what makes its errors usable as
    feedback to an agent.
    """
    check_document(doc)
    p = platform()
    fg = p.make_flow_graph()
    fg.import_data(doc)
    fg.rewrite()
    fg.validate()
    if not fg.is_valid():
        raise DocumentRejected([f"{name}: {m}" for m in fg.iter_error_messages()])
    return fg
