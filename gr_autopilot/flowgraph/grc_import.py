"""Reading a GNU Radio Companion file back in, so an edited flowgraph can actually be run.

Exporting a graph makes it inspectable. Reading one back makes it EDITABLE: open the exported
file in Companion, change the excess bandwidth, widen a loop, add carrier recovery, save, and
hand it back. The experiment then runs the graph you edited rather than the one it proposed.

How it works. The file is walked from its payload source to its sink, following the connections
rather than the order blocks happen to appear in the document, and each block is mapped back to
the skill it came from. Everything between the source and the channel is the transmit chain;
everything after the channel is the receive chain. Parameters that the running backend honours --
samples per symbol, excess bandwidth, timing loop bandwidth -- are read back out of the block
settings, including out of the ``firdes`` expression used for filter taps, so editing the number
in the editor genuinely changes the measurement.

What this deliberately does NOT do is execute arbitrary graphs. A block with no equivalent in the
skill vocabulary is reported by name rather than ignored, because silently dropping a block the
user deliberately added would run something other than what they asked for and report the result
as theirs. The honest answer to an unsupported block is to say which one and stop.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from gr_autopilot.flowgraph.spec import FlowgraphSpec

#: Companion block identifier -> the skill it realizes.
_BLOCK_TO_SKILL = {
    "digital_chunks_to_symbols_xx": "_modulator",     # resolved to a modulation below
    "interp_fir_filter_xxx": "rrc_pulse_shape",
    "fir_filter_xxx": "rrc_matched_filter",
    "digital_symbol_sync_xx": "symbol_sync",
    "digital_costas_loop_cc": "costas_carrier",
    "analog_agc2_xx": "agc",
}

#: Blocks that are part of the harness rather than the agent's chain.
_STRUCTURAL = {"blocks_vector_source_x", "blocks_vector_sink_x", "analog_noise_source_x",
               "blocks_add_xx", "blocks_rotator_cc", "variable", "blocks_null_sink",
               "blocks_throttle"}

_CHANNEL_BLOCK = "blocks_add_xx"

_MOD_BY_ORDER = {2: "bpsk", 4: "qpsk", 8: "8psk", 16: "16qam", 32: "32qam", 64: "64qam", 256: "256qam"}
_DEMOD_BY_MOD = {"bpsk": "bpsk_demod", "qpsk": "qpsk_demod", "8psk": "psk8_demod",
                 "16qam": "qam16_demod", "32qam": "qam32_demod", "64qam": "qam64_demod",
                 "256qam": "qam256_demod"}
_MODULATOR_BY_MOD = {m: d.replace("_demod", "_mod") for m, d in _DEMOD_BY_MOD.items()}


class UnsupportedFlowgraph(ValueError):
    """The file contains something this system cannot run, named explicitly."""


@dataclass
class ImportedFlowgraph:
    spec: FlowgraphSpec
    overrides: dict = field(default_factory=dict)   # sps, rolloff, timing_loop_bw
    source_path: str = ""
    sha256: str = ""
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"spec": self.spec.to_dict(), "overrides": dict(self.overrides),
                "source_path": self.source_path, "sha256": self.sha256,
                "warnings": list(self.warnings)}


def _numbers(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", str(text))]


def _rrc_params(taps_expression: str) -> tuple[int | None, float | None]:
    """Pull samples-per-symbol and excess bandwidth out of a taps expression.

    The exported form is ``firdes.root_raised_cosine(gain, sample_rate, symbol_rate, rolloff,
    ntaps)``; with the sample rate written in units of symbols, the second argument IS the number
    of samples per symbol. A hand-written expression that does not match is not an error -- the
    parameters simply cannot be read, and the caller keeps its defaults.
    """
    if "root_raised_cosine" not in str(taps_expression):
        return None, None
    nums = _numbers(str(taps_expression).split("root_raised_cosine", 1)[1])
    if len(nums) < 4:
        return None, None
    return int(nums[1]), float(nums[3])


def _ordered_chain(blocks: dict, connections: list) -> list[str]:
    """Block names from the source to the sink, following the wiring."""
    outgoing: dict[str, str] = {}
    has_input: set[str] = set()
    for src, _sp, dst, _dp in connections:
        # The noise generator also feeds the channel; the signal path is the one that starts at
        # the payload source, so the noise branch is skipped when choosing the successor.
        if blocks.get(src, {}).get("id") == "analog_noise_source_x":
            continue
        outgoing.setdefault(src, dst)
        has_input.add(dst)
    starts = [n for n, b in blocks.items()
              if b["id"] == "blocks_vector_source_x" and n not in has_input]
    if not starts:
        starts = [n for n in blocks if n not in has_input]
    if not starts:
        raise UnsupportedFlowgraph("the flowgraph has no starting block")

    order, seen, node = [], set(), starts[0]
    while node is not None and node not in seen:
        order.append(node)
        seen.add(node)
        node = outgoing.get(node)
    return order


def load_grc(path: str | Path) -> ImportedFlowgraph:
    """Read a Companion file into a runnable specification."""
    import yaml

    path = Path(path)
    raw = path.read_bytes()
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise UnsupportedFlowgraph(f"not a readable Companion file: {exc}") from exc
    if not isinstance(doc, dict) or "blocks" not in doc:
        raise UnsupportedFlowgraph("not a Companion file: no 'blocks' section")

    blocks = {b["name"]: b for b in doc.get("blocks") or []}
    connections = [list(c) for c in (doc.get("connections") or [])]
    order = _ordered_chain(blocks, connections)

    tx_chain: list[str] = []
    rx_chain: list[str] = []
    overrides: dict = {}
    warnings: list[str] = []
    modulation: str | None = None
    seen_channel = False
    current = tx_chain

    for name in order:
        block = blocks[name]
        block_id = block["id"]
        params = block.get("parameters") or {}

        if block_id == _CHANNEL_BLOCK:
            seen_channel = True
            current = rx_chain
            continue
        if block_id in _STRUCTURAL:
            continue

        skill = _BLOCK_TO_SKILL.get(block_id)
        if skill is None:
            raise UnsupportedFlowgraph(
                f"block {name!r} ({block_id}) has no equivalent in the skill vocabulary, so this "
                f"flowgraph cannot be run as written. Supported blocks: "
                f"{sorted(_BLOCK_TO_SKILL)}")

        if skill == "_modulator":
            order_n = len(_numbers(params.get("symbol_table", ""))) // 2   # complex pairs
            modulation = _MOD_BY_ORDER.get(order_n)
            if modulation is None:
                raise UnsupportedFlowgraph(
                    f"constellation of {order_n} points is not one of "
                    f"{sorted(_MOD_BY_ORDER.values())}")
            current.append(_MODULATOR_BY_MOD[modulation])
            continue

        if skill == "rrc_pulse_shape":
            sps, rolloff = _rrc_params(params.get("taps", ""))
            interp = params.get("interp")
            if interp is not None and _numbers(interp):
                sps = int(_numbers(interp)[0])
            if sps:
                overrides["sps"] = sps
            if rolloff is not None:
                overrides["rolloff"] = rolloff
        elif skill == "rrc_matched_filter":
            _sps, rolloff = _rrc_params(params.get("taps", ""))
            if rolloff is not None:
                overrides.setdefault("rolloff", rolloff)
        elif skill == "symbol_sync":
            bw = _numbers(params.get("loop_bw", ""))
            if bw:
                overrides["timing_loop_bw"] = bw[0]

        current.append(skill)

    if not seen_channel:
        warnings.append("no channel block found; everything was treated as the transmit chain")
    if modulation is None:
        raise UnsupportedFlowgraph("no constellation mapper found, so the modulation is unknown")

    # The demodulator is not drawn in an exported graph (the symbol decision belongs to the
    # scoring layer), so it is restored here to keep the specification valid.
    demod = _DEMOD_BY_MOD[modulation]
    if demod not in rx_chain:
        rx_chain.append(demod)

    structure_id = str((doc.get("options") or {}).get("parameters", {}).get("id") or "imported")
    sync = {"timing": "gardner"}
    if overrides.get("timing_loop_bw") is not None:
        sync["loop_bw"] = overrides["timing_loop_bw"]
    spec = FlowgraphSpec(
        structure_id=structure_id, modulation=modulation,
        tx_chain=tx_chain, rx_chain=rx_chain, sync=sync,
        pulse_shape={"sps": overrides.get("sps", 4),
                     "rolloff": overrides.get("rolloff", 0.35)})
    return ImportedFlowgraph(spec=spec, overrides=overrides, source_path=str(path),
                             sha256=hashlib.sha256(raw).hexdigest(), warnings=warnings)
