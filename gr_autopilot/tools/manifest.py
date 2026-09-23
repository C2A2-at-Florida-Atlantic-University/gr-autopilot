"""The §8 tool surface as a transport-agnostic manifest: one entry per tool with its name, an
LLM-facing description, a JSON-Schema for its arguments, and a handler bound to an
``AutopilotService`` method.

This is the single source of truth consumed by every adapter -- the no-dependency MCP stdio
server (``gr_autopilot.tools.mcp_stdio``) and any SDK-based one -- so the tool set can never
drift between "what the service can do" and "what an agent is offered". All logic stays in the
service; the manifest only names, documents, and marshals it (Marconi's "logic in the core,
marshalling-only adapter" pattern).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[Any, dict], Any]  # (AutopilotService, arguments) -> JSON-serializable

    def to_mcp(self) -> dict:
        """The `tools/list` shape an MCP client expects."""
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


def _obj(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties or {},
            "required": list(required or []), "additionalProperties": False}


# The whole-transceiver-as-JSON construction surface (see FlowgraphSpec). The agent authors this;
# list_skills() enumerates the chain vocabulary at runtime.
_SPEC_SCHEMA = {
    "type": "object",
    "description": "A whole transceiver as one object (spec §8). Compose tx/rx chains from list_skills().",
    "properties": {
        "structure_id": {"type": "string", "description": "stable id for this structure, e.g. 'qpsk_link'"},
        "modulation": {"type": "string",
                       "enum": ["bpsk", "qpsk", "8psk", "16qam", "32qam", "64qam", "256qam"],
                       "description": "structural modulation: 1/2/3/4/5/6/8 bits per symbol. "
                                      "The radios' error-vector floor decides how high the "
                                      "ladder really goes; 256-QAM is simulation-only."},
        "tx_chain": {"type": "array", "items": {"type": "string"},
                     "description": "ordered skill names, bits -> complex (e.g. ['qpsk_mod','rrc_pulse_shape'])"},
        "rx_chain": {"type": "array", "items": {"type": "string"},
                     "description": "ordered skill names, complex -> bits"},
        "pulse_shape": {"type": "object", "properties": {
            "type": {"type": "string"}, "sps": {"type": "integer"}, "rolloff": {"type": "number"}}},
        "sync": {"type": "object", "properties": {"timing": {"type": "string"}}},
        "coding": {"type": ["string", "null"], "description": "optional FEC label, or null (Stage 1)"},
    },
    "required": ["structure_id", "modulation", "tx_chain", "rx_chain"],
}

# What a connecting LLM client is handed on `initialize` (surfaced by good clients as guidance).
INSTRUCTIONS = """\
You are the outer-loop policy of gr-autopilot: an autonomous radio engineer building a GNU Radio
link over a real (or simulated) RF path. Objective: make the link meet its BER target while
MAXIMIZING spectral efficiency -- use the highest-order modulation (and least coding) that still
meets the target.

You are NOT told the channel SNR. Reason only from framework-graded measurements (get_metrics).

FIRST, before anything that measures or records: call current_experiment(). If nothing is selected,
ASK THE OPERATOR what this experiment should be called (and, briefly, what it is for) -- do not
invent a name -- then start_experiment(name, goal) for a new one or switch_experiment(name) to
reopen one (list_saved_experiments shows what exists). Every measuring tool refuses with
'no_experiment' until one is selected. Starting or switching RESETS the session and the radios
(structure, tuning, claims, hop plan, retune, and the interferer) and returns a report of what it
cleared; relay that report to the operator. rename_experiment changes the name without a reset.

A typical loop:
  1. describe_experiment('link_only') and get_rf_path_config() -- learn the goal and the RF path.
  2. list_skills() for the DSP vocabulary; claim_device the TX and RX radios (the grader is
     framework-reserved -- you cannot claim it).
  3. build_flowgraph(spec) for a modulation, then run_flowgraph() and get_metrics().
  4. If BER is comfortably below target, climb the ladder (bpsk -> qpsk -> 8psk -> 16qam -> 32qam
     -> 64qam) for efficiency; each rung needs roughly 3 dB more than the last, so on a real radio
     expect the climb to stop where the error-vector floor says, not at the top of the list;
     if a rung FAILS, call diagnose_signal to SEE why before reacting: 'phase_offset' -> start_bo_run
     (['phase_correction_rad'], [[-0.79, 0.79]]) to tune the carrier; 'carrier_unlocked' -> drop order;
     'low_snr' -> drop the ladder / add coding (BO will not rescue noise).
  5. Keep the highest-efficiency structure that meets the target. annotate_ledger with your reasoning.

Spectral efficiency in bits/symbol: 256qam 8 > 64qam 6 > 32qam 5 > 16qam 4 > 8psk 3 > qpsk 2 >
bpsk 1. Prefer the highest that meets BER.

REPORTING. Produce ONE report, at the very END, and only once ALL the measuring is done and
nothing further will be run. Do not emit interim report-shaped output -- no per-rung summaries, no
running tables, no "here is what we have so far". A partial answer invites the operator to act on
a number the rest of the run will move: a rung that pools under target over its first few
full-length trials can land well over it once the rest arrive. Work quietly; if something mid-run
genuinely needs saying, keep it to one line of status, not findings.

The last thing you do is, in this order:
  1. finish_experiment(summary) -- ONCE, after the final measurement, never between rungs or
     while anything is still to be run. This is what tells the console the run is over and
     raises its report; nothing can infer it, because a pause to look at a constellation is
     indistinguishable from an ending. Its return value is the run's outcome: trial counts,
     structures, the best graded trial, and every decision you took.
  2. Write your report, from that outcome plus your own measurements.

Let the request you were given set the report's shape -- re-read it and answer what it asked, in
the order it asked, rather than dumping everything you measured. Whatever the run was, the report
leads with its OUTCOME over the whole flow, not a log of steps: a ladder ends on the configuration
you settled on and what stopped the next one; an avoidance or hopping run ends on where the link
started, where it ended up, and the decisions between; a tuning run ends on the parameters you
kept and what they bought. The steps are evidence for that answer, not the answer.

Conditions can CHANGE between runs (SNR shifts, a jammer appears or starts following you) — always
re-measure and re-maximize. If BER fails at EVERY modulation on the current channel, the channel may
be interfered:
  * sense_spectrum([candidate freqs]) — look for a channel reading occupied (energy over noise).
  * If a candidate reads clear, set_center_freq(it) and re-run — frequency avoidance.
  * If every candidate senses clear but the link is STILL broken after retuning, the jammer is
    likely REACTIVE (it follows you). set_hop_plan([channels], hop_rate_hz) to hop faster than it
    can retune, raising the rate until BER meets; clear_hop_plan() once the threat is gone.
"""


def tool_manifest() -> list[Tool]:
    """Every §8 tool, in a sensible discovery-to-optimize order."""
    return [
        # -- experiments: the record everything below is written into ----------------
        Tool("current_experiment",
             "Which experiment is selected (name, goal, backend, directory, iteration count, the "
             "built structure and claims), or {selected:false} with the saved names. Call this "
             "FIRST. If nothing is selected, ask the operator for a name -- never invent one.",
             _obj(), lambda s, a: s.current_experiment()),
        Tool("list_saved_experiments",
             "Every experiment under the runs directory, newest activity first: name, slug "
             "(its directory), goal, backend, iteration count.",
             _obj(), lambda s, a: s.list_saved_experiments()),
        Tool("start_experiment",
             "Create a NEW experiment with the operator's name and make it current. RESETS the "
             "session and the radios (structure, tuning, claims, hop plan, retune, interferer) and "
             "returns a report of what was cleared -- relay it. Refuses a name that already exists.",
             _obj({"name": {"type": "string", "description": "the operator's name for it"},
                   "goal": {"type": "string", "description": "one line on what it is for"}},
                  ["name"]),
             lambda s, a: s.start_experiment(a["name"], a.get("goal", ""))),
        Tool("switch_experiment",
             "Reopen an EXISTING experiment and make it current; its history continues. RESETS "
             "the session and the radios exactly as start_experiment does and returns the report.",
             _obj({"name": {"type": "string"}}, ["name"]),
             lambda s, a: s.switch_experiment(a["name"])),
        Tool("rename_experiment",
             "Rename the CURRENT experiment (and its directory). Nothing is reset; optionally "
             "update its goal.",
             _obj({"name": {"type": "string"}, "goal": {"type": "string"}}, ["name"]),
             lambda s, a: s.rename_experiment(a["name"], a.get("goal"))),
        Tool("finish_experiment",
             "Declare the experiment CONCLUDED -- call this ONCE, after ALL measuring is done and "
             "immediately before you write your report, and never mid-run. It is the only "
             "end-of-run signal: it stamps the experiment ended and raises the operator console's "
             "run report, which cannot otherwise tell a finished run from a pause. Returns the "
             "run's outcome (trial counts, structures, the best graded trial, the decisions you "
             "took) so your report and the console's are built from the same numbers. Appending "
             "anything afterwards reopens the experiment, so if more measuring turns out to be "
             "needed, just carry on and finish again at the true end.",
             _obj({"summary": {"type": "string",
                               "description": "optional one line: what the run concluded"}}),
             lambda s, a: s.finish_experiment(a.get("summary", ""))),
        # ---- Discovery ----
        Tool("list_blocks",
             "List curated GNU Radio blocks; optionally filter by category or a search string.",
             _obj({"category": {"type": "string"}, "search": {"type": "string"}}),
             lambda s, a: s.list_blocks(a.get("category"), a.get("search"))),
        Tool("describe_block", "Full parameter and port detail for one block.",
             _obj({"name": {"type": "string"}}, ["name"]),
             lambda s, a: s.describe_block(a["name"])),
        Tool("list_skills",
             "List the composable DSP skills usable in tx_chain/rx_chain (the flowgraph vocabulary).",
             _obj(), lambda s, a: s.list_skills()),
        Tool("describe_skill", "Ports and parameters of one skill.",
             _obj({"name": {"type": "string"}}, ["name"]),
             lambda s, a: s.describe_skill(a["name"])),
        Tool("list_devices", "List the radios visible to the framework.",
             _obj(), lambda s, a: s.list_devices()),
        Tool("probe_device", "Capabilities of one device (sample-rate range, tuning range, gains).",
             _obj({"device_id": {"type": "string"}}, ["device_id"]),
             lambda s, a: s.probe_device(a["device_id"])),
        Tool("get_rf_path_config", "The fixed RF path between TX and RX (cabling + attenuators).",
             _obj(), lambda s, a: s.get_rf_path_config()),
        Tool("list_experiments",
             "Experiment DEFINITIONS the framework offers (link_only, link_adaptive): the task and "
             "its success criterion. Not your saved experiments -- see list_saved_experiments.",
             _obj(), lambda s, a: s.list_experiments()),
        Tool("describe_experiment",
             "Participants, metrics, and success criterion of an experiment DEFINITION.",
             _obj({"name": {"type": "string"}}, ["name"]),
             lambda s, a: s.describe_experiment(a["name"])),
        # ---- Construct / Claim / Execute ----
        Tool("validate",
             "Validate a whole-flowgraph spec (skills exist, ports chain, params in range) WITHOUT running it.",
             _obj({"spec": _SPEC_SCHEMA}, ["spec"]),
             lambda s, a: s.validate(a["spec"])),
        Tool("build_flowgraph",
             "Commit a validated flowgraph spec as the current structure to run. Resets tuning.",
             _obj({"spec": _SPEC_SCHEMA}, ["spec"]),
             lambda s, a: s.build_flowgraph(a["spec"])),
        Tool("claim_device",
             "Claim a radio for a role. The grader is framework-reserved and cannot be claimed.",
             _obj({"device_id": {"type": "string"},
                   "role": {"type": "string", "enum": ["transmitter", "receiver"]}},
                  ["device_id", "role"]),
             lambda s, a: s.claim_device(a["device_id"], a["role"])),
        Tool("run_flowgraph",
             "Run the built flowgraph over the link and grade it (framework-owned). Read results with get_metrics.",
             _obj({"n_bits": {"type": "integer", "minimum": 1000}}),
             lambda s, a: s.run_flowgraph(a.get("n_bits"))),
        Tool("get_status",
             "Current session state (built structure, tuning, claims). Never reveals the hidden channel condition.",
             _obj(), lambda s, a: s.get_status()),
        # ---- Measure (framework-owned) ----
        Tool("get_metrics",
             "Framework-graded BER, EVM (%), and estimated SNR of the last run, with the bits "
             "graded and the errors counted so ratios can be POOLED (total errors / total bits) "
             "rather than averaged. After a hopped run it also carries `hop`: the measured dwell "
             "and hop rate against the requested ones, and a per-dwell row (channel, graded bits, "
             "errors, BER, EVM, SNR) so the spread across dwells and the fraction of dwells hit "
             "are measurements rather than inferences. The true channel SNR is never exposed.",
             _obj(), lambda s, a: s.get_metrics()),
        Tool("capture_constellation", "Up to max_points recovered RX symbols as [I,Q] pairs.",
             _obj({"max_points": {"type": "integer", "minimum": 1, "maximum": 4096}}),
             lambda s, a: s.capture_constellation(a.get("max_points", 256))),
        Tool("capture_spectrum",
             "PSD summary (peak, noise floor) of the last run's RECOVERED SYMBOLS — one sample per "
             "symbol, after matched filtering and timing recovery. It is not an RF spectrum: the "
             "pulse shaping has already been removed, so occupied bandwidth and rolloff are not "
             "observable here.",
             _obj(), lambda s, a: s.capture_spectrum()),
        Tool("diagnose_signal",
             "SEE the signal: diagnose the last run's recovered constellation into a named fault "
             "(clean / phase_offset / carrier_unlocked / low_snr) with the fix — telling a phase "
             "rotation (tune the carrier) from low SNR (drop the rate) from a spinning ring (carrier "
             "unlocked), which a scalar BER cannot. Optionally render the scope PNG for a vision model.",
             _obj({"render_path": {"type": "string",
                                   "description": "optional path to write the constellation PNG"}}),
             lambda s, a: s.diagnose_signal(a.get("render_path"))),
        # ---- Sense / retune / hop (Stage 2-3: adapt to interference) ----
        Tool("sense_spectrum",
             "Monitor role: energy-detect received power over noise at each candidate center "
             "frequency (link silent) to locate a jammer. A REACTIVE jammer reads clear here — it "
             "only fires when you transmit. Never reveals the hidden channel SNR.",
             _obj({"freqs_hz": {"type": "array", "items": {"type": "number"},
                                "description": "candidate link center frequencies to scan (Hz)"},
                   "bw_hz": {"type": "number", "description": "sensing bandwidth (Hz), optional"}},
                  ["freqs_hz"]),
             lambda s, a: s.sense_spectrum(a["freqs_hz"], a.get("bw_hz"))),
        Tool("set_center_freq",
             "Retune the link to a center frequency (frequency avoidance). Persists until changed; "
             "the next run_flowgraph uses it.",
             _obj({"center_freq_hz": {"type": "number"}}, ["center_freq_hz"]),
             lambda s, a: s.set_center_freq(a["center_freq_hz"])),
        Tool("set_hop_plan",
             "Hop the link across a channel set at hop_rate_hz to out-run a channel-following "
             "(reactive) jammer. Raise the rate until BER meets; a fast enough follower still wins. "
             "On a radio the link physically retunes every dwell, so the dwell you get is bounded "
             "below by how long one graded capture takes: get_metrics returns the MEASURED dwell "
             "and hop rate beside the requested ones, plus a per-dwell grade for the cycle. Report "
             "the measured figures. Refused outright on a backend that cannot hop.",
             _obj({"channels_hz": {"type": "array", "items": {"type": "number"}},
                   "hop_rate_hz": {"type": "number", "minimum": 0}},
                  ["channels_hz", "hop_rate_hz"]),
             lambda s, a: s.set_hop_plan(a["channels_hz"], a["hop_rate_hz"])),
        Tool("clear_hop_plan", "Stop hopping and sit on the current center frequency (threat gone).",
             _obj(), lambda s, a: s.clear_hop_plan()),
        Tool("set_tx_power",
             "Power adaptation: set TX power (dB, relative) within the power budget. Back OFF to "
             "transmit no louder than needed (efficiency), or BOOST to hold a higher modulation or "
             "overcome a jammer. Clamped to a finite PA budget; the path loss stays hidden, so find "
             "the power you need by measurement.",
             _obj({"power_db": {"type": "number"}}, ["power_db"]),
             lambda s, a: s.set_tx_power(a["power_db"])),
        # ---- Learn (self-improvement: author + promote skills) ----
        Tool("author_skill",
             "SELF-IMPROVE: experiment on the grader at the current channel to discover a new building "
             "block — benchmark a ladder of modcods (incl. coded rungs) and, if a coded rung fills an "
             "efficiency gap the base rungs cannot at this SNR, propose it as a learned skill (not "
             "promoted yet).",
             _obj({"name": {"type": "string", "description": "name for the new skill"},
                   "ladder": {"type": "array", "items": {"type": "string"},
                              "description": "candidate modcods, e.g. ['qpsk','16qam','16qam:conv_k3_r34']"},
                   "target_ber": {"type": "number"}},
                  ["name"]),
             lambda s, a: s.author_skill(a["name"], a.get("ladder"), a.get("target_ber", 1e-2))),
        Tool("promote_skill",
             "Test-gate the last authored proposal and, on pass, PERSIST it into your vocabulary (it "
             "must re-validate on a fresh grader trial and beat its baseline efficiency by a margin); "
             "list_skills then includes it and you can build_flowgraph with its spec.",
             _obj({"name": {"type": "string"}, "min_gain": {"type": "number"}}),
             lambda s, a: s.promote_skill(a.get("name"), a.get("min_gain", 0.4))),
        # ---- Optimize (BO inner loop) / Memory ----
        Tool("start_bo_run",
             "Run the Bayesian-optimization inner loop to tune continuous knobs on the built structure. "
             "Optionally stop early once target_ber is met. Each trial is the mean of `repeats` "
             "measurements and the winner is re-measured: check `reproduced` and `validated_loss` "
             "before trusting `best_loss`, because a single noisy run per trial lets the search "
             "optimise the noise instead of the knob.",
             _obj({"params": {"type": "array",
                              "items": {"type": "string", "enum": ["phase_correction_rad", "rolloff"]}},
                   "bounds": {"type": "array", "items": {
                       "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}},
                   "budget": {"type": "integer", "minimum": 1, "maximum": 100},
                   "target_ber": {"type": "number"},
                   "trial_bits": {"type": "integer", "minimum": 1000},
                   "repeats": {"type": "integer", "minimum": 1, "maximum": 10,
                               "description": "measurements averaged per trial (default 3)"}},
                  ["params", "bounds"]),
             lambda s, a: s.start_bo_run(a["params"], a["bounds"], a.get("budget", 12),
                                         a.get("target_ber"), a.get("trial_bits", 40000),
                                         a.get("repeats", 3))),
        Tool("get_bo_status", "Best params and loss from the last BO run.",
             _obj(), lambda s, a: s.get_bo_status()),
        Tool("stop_bo_run", "Stop the BO inner loop.",
             _obj(), lambda s, a: s.stop_bo_run()),
        Tool("read_edit_ledger",
             "The framework's append-only edit ledger (your iteration history). compact=true returns a table.",
             _obj({"compact": {"type": "boolean"}}),
             lambda s, a: s.read_edit_ledger(a.get("compact", True))),
        Tool("export_flowgraph",
             "Write the current flowgraph as a GNU Radio Companion (.grc) file the operator can "
             "open, inspect, edit and run. On the gr-spec backend every block in it is a block this "
             "experiment actually instantiates; the Pluto backend runs a fixed receiver and honours "
             "only modulation, rolloff, sps and coding, so the file then records the REQUESTED "
             "structure. Every structure that runs is already exported to the experiment's "
             "flowgraphs/ directory; call this to re-export the current graph or write it "
             "elsewhere. Returns the path.",
             _obj({"path": {"type": "string",
                            "description": "optional destination; defaults to the run's "
                                           "flowgraphs/ directory"}}),
             lambda s, a: s.export_flowgraph(a.get("path"))),
        Tool("import_flowgraph",
             "Load a GNU Radio Companion (.grc) file and run THAT graph from now on — the other "
             "half of export_flowgraph, for a graph the operator has edited by hand. Reads back "
             "the settings the backend honours (samples per symbol, excess bandwidth, timing loop "
             "bandwidth). A block with no equivalent in the skill vocabulary is refused by name "
             "rather than silently dropped. Runs from an imported graph are recorded as "
             "hand-edited, with the file's content hash.",
             _obj({"path": {"type": "string", "description": "path to the .grc file"}}, ["path"]),
             lambda s, a: s.import_flowgraph(a["path"])),
        Tool("annotate_ledger", "Attach a note (your reasoning) to a ledger iteration.",
             _obj({"iteration": {"type": "integer"}, "note": {"type": "string"}}, ["iteration", "note"]),
             lambda s, a: s.annotate_ledger(a["iteration"], a["note"])),
    ]
