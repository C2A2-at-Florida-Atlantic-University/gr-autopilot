"""Exporting an experiment's flowgraph as a GNU Radio Companion (``.grc``) file.

GNU Radio Companion is the graphical editor shipped with GNU Radio. Its saved files are YAML
documents listing blocks, their parameters, and the connections between them. This module writes
one for a given flowgraph specification, so that the graph an agent arrived at is something a
person can open, read, modify, and run.

This export is FAITHFUL, and that word is load-bearing. Every block written here is a block that
:mod:`gr_autopilot.link.gr_spec` actually instantiates when it runs the experiment, with the same
parameters -- the same filter taps, the same loop bandwidth, the same decimation. Exporting a
picture of a signal chain that the measurement did not actually use would be worse than exporting
nothing, because it would attribute results to a structure that never ran.

That guarantee is a property of the EXECUTING backend. :class:`~gr_autopilot.link.pluto.PlutoBackend`
runs a fixed tracking receiver and reads only ``modulation``, ``rolloff``, ``sps`` and ``coding``
from the specification, so an export taken from a hardware run records the structure the agent
REQUESTED, not a graph that ran on the radios; the tool description says so, and the experiment's
``backend`` field is the record of what actually measured it.

Two departures from the executed graph are unavoidable, and both are written into the file as
comments rather than left for the reader to discover:

* The payload source is a finite vector of symbol indices held in memory during the experiment.
  A file that inlined tens of thousands of integers would be unreadable, so the exported source
  carries a short excerpt and a note.
* The symbol decision and the bit-error count belong to the scoring layer, which owns the known
  payload; they are not blocks. The exported graph therefore ends at recovered symbols.

The result is a runnable, editable starting point rather than a byte-exact replica of an
internal measurement harness.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gr_autopilot import modulation
from gr_autopilot.flowgraph.gr_blocks import FRAMEWORK_REALIZED, GraphContext

_GRID_X, _GRID_Y = 220, 140      # spacing of the block layout, in Companion's coordinate units


def _q(value) -> str:
    """Companion stores every parameter as a string, including numbers."""
    return str(value)


@dataclass
class _Node:
    name: str
    block_id: str
    parameters: dict
    comment: str = ""

    def to_dict(self, index: int, row: int = 0) -> dict:
        if self.block_id == "variable":     # variables carry no stream/scheduler parameters
            params = {"comment": self.comment}
        else:
            params = {"affinity": "", "alias": "", "comment": self.comment,
                      "maxoutbuf": "0", "minoutbuf": "0"}
        params.update({k: _q(v) for k, v in self.parameters.items()})
        return {"name": self.name, "id": self.block_id, "parameters": params,
                "states": {"coordinate": [40 + index * _GRID_X, 40 + row * _GRID_Y],
                           "rotation": 0, "state": "enabled"}}


def _taps_expression(ctx: GraphContext) -> str:
    """A readable, editable expression for the pulse-shaping filter, rather than a wall of
    numbers -- so that changing the excess bandwidth in the editor actually recomputes the taps."""
    return (f"firdes.root_raised_cosine(1.0, {float(ctx.sps)}, 1.0, "
            f"{ctx.rolloff}, {ctx.ntaps_per_symbol * ctx.sps + 1})")


def _node_for(skill: str, ctx: GraphContext, cur_sps: float, index: int):
    """The Companion equivalent of one skill. Returns ``(node, rate_out_per_in)``."""
    const = ctx.constellation
    if skill.endswith("_mod"):
        table = ", ".join(f"({p.real:.6g}{p.imag:+.6g}j)" for p in const.points)
        return _Node(f"mod_{index}", "digital_chunks_to_symbols_xx",
                     {"in_type": "byte", "out_type": "complex",
                      "symbol_table": f"[{table}]", "dimension": 1, "num_ports": 1},
                     comment=f"{ctx.modulation.upper()} constellation mapper"), 1.0
    if skill == "rrc_pulse_shape":
        return _Node(f"pulse_shape_{index}", "interp_fir_filter_xxx",
                     {"type": "ccf", "interp": ctx.sps, "taps": _taps_expression(ctx),
                      "samp_delay": 0},
                     comment="root-raised-cosine transmit filter"), float(ctx.sps)
    if skill == "rrc_matched_filter":
        decim = max(1, int(cur_sps // 2))
        return _Node(f"matched_filter_{index}", "fir_filter_xxx",
                     {"type": "ccf", "decim": decim, "taps": _taps_expression(ctx),
                      "samp_delay": 0},
                     comment=("matched receive filter; also decimates to 2 samples per symbol, "
                              "which is what the timing recovery downstream expects")), 1.0 / decim
    if skill == "symbol_sync":
        sps_in = max(2.0, float(cur_sps))
        return _Node(f"symbol_sync_{index}", "digital_symbol_sync_xx",
                     {"type": "cc", "ted_type": "digital.TED_GARDNER",
                      "constellation": "digital.constellation_qpsk().base()",
                      "sps": sps_in, "ted_gain": 1.0, "loop_bw": round(ctx.timing_loop_bw, 6),
                      "damping": 1.0, "max_dev": 1.5, "osps": 1,
                      "resamp_type": "digital.IR_MMSE_8TAP", "nfilters": 128,
                      "pfb_mf_taps": "[]"},
                     comment="symbol timing recovery"), 1.0 / sps_in
    if skill == "costas_carrier":
        order = {"bpsk": 2, "qpsk": 4, "8psk": 8}.get(ctx.modulation, 4)
        return _Node(f"costas_{index}", "digital_costas_loop_cc",
                     {"w": round(ctx.carrier_loop_bw, 6), "order": order, "use_snr": "False"},
                     comment="carrier phase and frequency recovery"), 1.0
    if skill == "agc":
        return _Node(f"agc_{index}", "analog_agc2_xx",
                     {"type": "complex", "attack_rate": 0.001, "decay_rate": 0.001,
                      "reference": 1.0, "gain": 1.0, "max_gain": 65536},
                     comment="automatic gain control"), 1.0
    if skill in FRAMEWORK_REALIZED:
        return None, 1.0
    raise ValueError(f"no GNU Radio Companion block is defined for skill {skill!r}")


def build_grc(spec, *, es_n0_db=None, noise_amplitude=0.1, seed=1, sample_rate=1_000_000.0,
              freq_offset_hz=0.0, title=None, ntaps_per_symbol=8,
              timing_loop_bw=None) -> dict:
    """Assemble the Companion document for ``spec`` as a plain dictionary."""
    ps = spec.pulse_shape or {}
    ctx = GraphContext(modulation=spec.modulation, sps=int(ps.get("sps", 4)),
                       rolloff=float(ps.get("rolloff", 0.35)),
                       ntaps_per_symbol=int(ntaps_per_symbol))
    if timing_loop_bw is not None:
        ctx.timing_loop_bw = float(timing_loop_bw)
    elif (spec.sync or {}).get("loop_bw") is not None:
        # Round-trip: a value the operator edited into a previous export is carried on the
        # specification, and must be written back out rather than silently reset to the default.
        try:
            ctx.timing_loop_bw = float(spec.sync["loop_bw"])
        except (TypeError, ValueError):
            pass

    const = modulation.get(spec.modulation)
    nodes: list[tuple[_Node, int]] = []       # (node, row)
    connections: list[list[str]] = []

    src = _Node("payload_source", "blocks_vector_source_x",
                {"type": "byte", "vector": "[0, 1, 2, 3]", "tags": "[]", "repeat": "True",
                 "vlen": 1},
                comment=("EXPORT NOTE: during the experiment this carries the framework's known "
                         "pseudo-random payload as one integer per symbol. A short repeating "
                         "excerpt is used here so the file stays readable."))
    nodes.append((src, 0))
    prev = src.name

    index, rate = 0, 1.0
    for skill in spec.tx_chain:
        node, r = _node_for(skill, ctx, rate, index)
        if node is None:
            continue
        index += 1
        rate *= r
        nodes.append((node, 0))
        connections.append([prev, "0", node.name, "0"])
        prev = node.name

    # -- the framework-owned channel -----------------------------------------
    if freq_offset_hz:
        # Expressed through variables so the offset can be changed in the editor without
        # recomputing anything by hand. Written as plain arithmetic on those variables: the
        # editor evaluates parameters in a namespace that does not include the ``math`` module,
        # so ``math.pi`` there is a load error rather than a value.
        nodes.append((_Node("samp_rate", "variable", {"value": _q(float(sample_rate))},
                            comment="sample rate (Hz)"), 2))
        nodes.append((_Node("freq_offset_hz", "variable", {"value": _q(float(freq_offset_hz))},
                            comment="transmitter/receiver carrier offset (Hz)"), 2))
        rot = _Node("carrier_offset", "blocks_rotator_cc",
                    {"phase_inc": "2*3.141592653589793*freq_offset_hz/samp_rate",
                     "tag_inc_update": "False"},
                    comment="carrier frequency offset between transmitter and receiver")
        index += 1
        nodes.append((rot, 0))
        connections.append([prev, "0", rot.name, "0"])
        prev = rot.name

    noise_comment = "additive white Gaussian noise"
    if es_n0_db is not None:
        noise_comment += (f" — amplitude set by the framework for the requested channel quality "
                          f"of {es_n0_db} dB (energy per symbol to noise density)")
    noise = _Node("channel_noise", "analog_noise_source_x",
                  {"type": "complex", "noise_type": "analog.GR_GAUSSIAN",
                   "amp": round(float(noise_amplitude), 8), "seed": int(seed)},
                  comment=noise_comment)
    adder = _Node("channel", "blocks_add_xx",
                  {"type": "complex", "num_inputs": 2, "vlen": 1},
                  comment="framework-owned channel: the agent does not place this")
    index += 1
    nodes.append((noise, 1))
    nodes.append((adder, 0))
    connections.append([prev, "0", adder.name, "0"])
    connections.append([noise.name, "0", adder.name, "1"])
    prev = adder.name

    for skill in spec.rx_chain:
        node, r = _node_for(skill, ctx, rate, index)
        if node is None:
            continue
        index += 1
        rate *= r
        nodes.append((node, 0))
        connections.append([prev, "0", node.name, "0"])
        prev = node.name

    sink = _Node("recovered_symbols", "blocks_vector_sink_x",
                 {"type": "complex", "vlen": 1, "reserve_items": 1024},
                 comment=("EXPORT NOTE: the graph ends at recovered symbols. The symbol decision "
                          "and the bit-error count belong to the scoring layer, which holds the "
                          "known payload, and are not blocks."))
    index += 1
    nodes.append((sink, 0))
    connections.append([prev, "0", sink.name, "0"])

    # Companion requires the flowgraph identifier to be a valid Python identifier: letters,
    # digits and underscores, beginning with a LETTER. Structure names here are routinely named
    # after a modulation ("16qam_link"), which begins with a digit, so a prefix is added when
    # needed rather than letting the file fail to load.
    flow_id = "".join(c if c.isalnum() else "_" for c in (spec.structure_id or "flowgraph"))
    if not flow_id or not flow_id[0].isalpha():
        flow_id = f"fg_{flow_id}"
    described = " -> ".join(list(spec.tx_chain) + ["channel"] + list(spec.rx_chain))
    options = {
        "parameters": {
            "author": "gr-autopilot", "category": "Custom", "cmake_opt": "", "comment": "",
            "copyright": "", "description": f"Exported from gr-autopilot: {described}",
            "gen_cmake": "On", "gen_linking": "dynamic", "generate_options": "no_gui",
            "hier_block_src_path": ".:", "id": flow_id, "max_nouts": "0",
            "output_language": "python", "placement": "(0,0)", "qt_qss_theme": "",
            "realtime_scheduling": "", "run": "True",
            "run_command": "{python} -u {filename}", "run_options": "run",
            "sizing_mode": "fixed", "thread_safe_setters": "",
            "title": title or f"gr-autopilot: {spec.structure_id}",
            "window_size": "1280, 1024",
        },
        "states": {"coordinate": [10, 10], "rotation": 0, "state": "enabled"},
    }
    return {
        "options": options,
        "blocks": [n.to_dict(i, row) for i, (n, row) in enumerate(nodes)],
        "connections": connections,
        "metadata": {"file_format": 1, "grc_version": "3.10.7.0"},
    }


def render_grc(spec, **kwargs) -> str:
    """The Companion document for ``spec`` as text. Deterministic for a given spec and settings,
    so two renderings can be compared to tell whether a file on disk already holds this graph."""
    import yaml
    return yaml.safe_dump(build_grc(spec, **kwargs), sort_keys=False,
                          default_flow_style=False, width=100)


def write_grc(spec, path: str | Path, **kwargs) -> Path:
    """Write the Companion document for ``spec`` to ``path``. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_grc(spec, **kwargs), encoding="utf-8")
    return path
