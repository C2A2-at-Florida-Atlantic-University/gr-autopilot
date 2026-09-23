"""AutopilotService — the §8 tool surface wired to the framework, integrity split enforced.

Design invariants (spec §2, §7):
  * The channel condition (SNR, phase offset) is held privately and NEVER returned to the
    agent — the agent reasons from measurements only (the "not told the SNR" claim).
  * BER/EVM/SNR are computed by the framework-owned scoring path on request; there is no
    tool that mutates scoring or fabricates the payload.
  * The framework pre-reserves the grader device; the agent cannot claim it.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from gr_autopilot.blocks.registry import BlockRegistry, default_registry
from gr_autopilot.control.objective import bo_loss
from gr_autopilot.control.skill_author import SkillAuthor
from gr_autopilot.flowgraph import FlowgraphSpec, build_and_run, validate_spec
from gr_autopilot.flowgraph.learned import LearnedSkillStore
from gr_autopilot.flowgraph.skills_registry import SkillsRegistry, default_skills
from gr_autopilot.hardware import (
    ClaimError,
    ClaimRegistry,
    Role,
    detect_bench,
    detect_devices,
    probe_device,
)
from gr_autopilot.hardware.supervisor import DeviceUnavailable
from gr_autopilot.ledger import EditLedger, LedgerEntry
import logging

from gr_autopilot.ledger.manager import ExperimentError, ExperimentManager, slugify
from gr_autopilot.link.backend import LinkBackend, LinkParams
from gr_autopilot.link.numpy_sim import NumpySimBackend
from gr_autopilot.optimize import run_bo
from gr_autopilot.scoring.metrics import compute_metrics

# Continuous knobs the agent may tune via the BO inner loop (spec §4.2), with default bounds.
TUNABLE_KNOBS = {"phase_correction_rad": (-0.7854, 0.7854), "rolloff": (0.05, 0.95)}

# The agent's TX power knob range (dB, relative): back off to transmit no louder than necessary, or
# boost to hold a higher rung / overcome a jammer — up to a finite power budget (a real PA ceiling).
TX_POWER_BUDGET_DB = (-20.0, 6.0)

# Ledger verdicts that mark a DECISION rather than a measurement: something the agent changed
# about the link. They carry no metrics, so nothing that pools error ratios picks them up, and
# they are what lets a report say why the channel moved between two trials.
_DECISION_VERDICTS = frozenset({"retuned", "hopping", "hopping_cleared", "power", "sensed",
                                "conclusion"})

_log = logging.getLogger("gr_autopilot.service")


def _grc_stem(structure_id: str | None) -> str:
    """A file name for a structure's exported graph, kept inside the flowgraphs directory.

    The structure id is agent-chosen free text, so a separator or a leading dot must not reach
    the filesystem: ``../x`` would otherwise write outside the experiment's directory. Ordinary
    ids ("64qam_link", "bpsk-probe.2") come through unchanged."""
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", structure_id or "").lstrip(".")[:120]
    return stem or "flowgraph"


def _reset_line(reset: dict) -> str:
    """The reset report as one log line."""
    parts = [*reset.get("session", []), *reset.get("hardware", []), *reset.get("operator", [])]
    return "; ".join(parts) if parts else "nothing to clear"

class ToolError(Exception):
    """A structured, agent-actionable tool error."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class AutopilotService:
    """Session state + the §8 tools. Methods return JSON-serializable dicts."""

    def __init__(self, backend: LinkBackend | None = None, ledger_path=None,
                 channel: dict | None = None, blocks: BlockRegistry | None = None,
                 confirm_bits: int = 200_000, device_backend: str = "mock",
                 tx_device: str | None = None, rx_device: str | None = None,
                 device_uris: dict | None = None, topology=None,
                 topology_findings: list | None = None,
                 experiment: str | None = "session", experiment_goal: str = "",
                 experiment_backend: str = "", runs_dir=None):
        self.blocks = blocks or default_registry()
        self.skills = default_skills()
        # "mock" (no radios) or "pluto" (live libiio scan) -> the same tool surface either way.
        # With device_uris the operator has named which radio holds which role, so the pair is
        # bound by URI: discovery order does not decide the roles, and role ids are assigned
        # rather than derived from a serial prefix two units of one batch can share.
        self.device_backend = device_backend
        if device_uris and device_backend != "mock":
            devs = detect_bench(device_uris["tx"], device_uris["rx"])
        else:
            devs = detect_devices(device_backend)
        self.devices = {d.device_id: d for d in devs}
        if len(self.devices) != len(devs):
            # A dict keyed by device id would otherwise drop the duplicate and leave one radio
            # silently playing both roles.
            raise RuntimeError(
                f"device detection returned {len(devs)} devices with only "
                f"{len(self.devices)} distinct ids: {[d.device_id for d in devs]}")
        ids = list(self.devices)
        self.tx_device = tx_device or ids[0]
        self.rx_device = rx_device or (ids[1] if len(ids) > 1 else ids[0])
        self.claims = ClaimRegistry(set(self.devices))
        # Framework owns the grader on the RX device (integrity split, spec §2/§5.3).
        self.claims.framework_reserve(self.rx_device, Role.GRADER)
        self.backend = backend or NumpySimBackend()
        # Experiments live under one runs directory, one subdirectory each (ledger/manager.py).
        # GR_AUTOPILOT_RUNS_DIR lets tests and CI redirect it to a temp dir.
        _runs = Path(runs_dir or os.environ.get("GR_AUTOPILOT_RUNS_DIR") or (Path.cwd() / "runs"))
        self.manager = ExperimentManager(_runs)
        # Provenance, recorded WITH the numbers: whether they came from radios or a model is not
        # recoverable from a BER afterwards.
        self.experiment_backend = experiment_backend
        # Operator-side hook run on every experiment switch (the daemon disarms the jammer here).
        # Returns a list of human-readable lines describing what it did.
        self.on_reset = None
        self.ledger: EditLedger | None = None
        self._learned = LearnedSkillStore(None)
        if ledger_path:
            # Compatibility: scripts and the old --ledger flag name the directory directly. The
            # directory's own name is the experiment; the runs dir is wherever that directory is.
            _lp = Path(ledger_path)
            self.manager = ExperimentManager(_lp.parent.parent if _lp.parent.name else _runs)
            # The directory IS the name unless the caller named it explicitly; the bare default
            # "session" is not an explicit name, it is the absence of one.
            explicit = experiment if (experiment and experiment != "session") else None
            self.ledger = EditLedger(_lp, experiment=explicit or _lp.parent.name or "session",
                                     goal=experiment_goal, backend=experiment_backend)
            self._learned = LearnedSkillStore(_lp.parent / "learned_skills.jsonl")
        elif experiment is not None:
            # A named default (library use, tests): create it or reopen it. ``None`` -- the
            # daemon's default -- leaves NO experiment selected, and every measuring tool refuses
            # until the operator or the agent names one.
            slug = slugify(experiment)
            if self.manager.db_path(slug).exists():
                self.ledger, _ = self.manager.open(slug, backend=experiment_backend)
            else:
                self.ledger, _ = self.manager.create(experiment, goal=experiment_goal,
                                                     backend=experiment_backend)
            self._learned = LearnedSkillStore(self.manager.learned_path(slug))
        self._last_proposal = None
        self._last_author_target = 1e-2
        self.confirm_bits = confirm_bits

        # PRIVATE — the operator/framework channel condition. Never exposed to the agent. For a
        # hardware backend it is device state (gains); for sim it rides in LinkParams.
        self._channel = dict(channel or ({} if self.backend.owns_channel else {"es_n0_db": 12.0}))
        # The declared bench, when one was loaded. ``topology_findings`` is the tier-A cross-check
        # result: a non-empty list means the hardware disagrees with the declaration, and
        # experiments are refused rather than producing data from a bench nobody can describe.
        self.topology = topology
        self.topology_findings = list(topology_findings or [])
        if topology is not None:
            self._rf_path = topology.rf_path_config()
        else:
            self._rf_path = {
                "path": f"{self.tx_device}.tx -> 20 dB attenuator -> {self.rx_device}.rx",
                "nominal_path_loss_db": 20.0, "medium": "coax + attenuators",
            }
        self._spec: FlowgraphSpec | None = None
        self._tuning: dict = {}
        # Agent RF actions (Stage 2-3): the link center frequency it retunes to (frequency
        # avoidance) and any hop plan it sets (hopping). These are the AGENT's own knobs — separate
        # from the hidden framework channel — and are applied to the backend on every run.
        self._agent_rf: dict = {}
        self._last_metrics = None
        self._last_result = None
        self._last_diagnosis = None
        self._bo_status = None

    # ---- experiments: naming, switching, what a switch resets --------------

    @property
    def experiment_dir(self) -> Path | None:
        return self.ledger.path.parent if self.ledger is not None else None

    @property
    def ledger_db_path(self) -> Path | None:
        return self.ledger.path if self.ledger is not None else None

    @property
    def artifacts_dir(self) -> Path | None:
        """Where exported GNU Radio Companion files go: beside the iteration history, so an
        experiment's artefacts stay together."""
        d = self.experiment_dir
        return d / "flowgraphs" if d is not None else None

    @property
    def snapshot_path(self) -> Path | None:
        d = self.experiment_dir
        return d / "telemetry.json" if d is not None else None

    def _require_experiment(self) -> None:
        if self.ledger is None:
            raise ToolError(
                "no_experiment",
                "No experiment is selected, so there is nowhere to record this. Ask the operator "
                "what this experiment should be called (do not invent a name), then call "
                "start_experiment(name, goal) to create it or switch_experiment(name) to reopen "
                "one; list_saved_experiments shows what already exists.")

    def current_experiment(self) -> dict:
        """What is selected right now, or ``{"selected": false}`` with what to do about it."""
        if self.ledger is None:
            # Names alone can collide (two rows both called "session" from before renaming
            # existed); the slug is the directory and is what switch_experiment resolves.
            return {"selected": False, "runs_dir": str(self.manager.runs_dir),
                    "saved": [{"name": i.name, "slug": i.slug, "iterations": i.iterations}
                              for i in self.manager.list()],
                    "next": "ask the operator for a name, then start_experiment or switch_experiment"}
        info = self.manager.info(self.ledger.path.parent.name)
        d = info.to_dict() if info else {"name": self.ledger.path.parent.name}
        return {"selected": True, **d,
                "structure_id": self._spec.structure_id if self._spec else None,
                "claims": self.claims.list_claims()}

    def list_saved_experiments(self) -> list:
        return [i.to_dict() for i in self.manager.list()]

    def _reset_session(self, reason: str) -> dict:
        """Everything a change of experiment must clear, and a plain statement of what was done.

        Session state first: the built structure, tuning, the last result and the BO status
        belong to the experiment that is ending. The agent's claims are released, because a claim
        is a statement about THIS experiment's roles. Then the radios: hop plan cleared, the
        agent's retune and power settings dropped so the next run starts from the bench's
        declared channel, and the hidden condition re-applied. Finally the operator hook, which
        the daemon binds to disarming the interferer -- a new experiment must not inherit a keyed
        transmitter it did not ask for. Every step is reported back, so the operator and the agent
        see the same account of what changed.
        """
        session, hardware, operator = [], [], []
        if self._spec is not None:
            session.append(f"built structure {self._spec.structure_id!r} discarded")
        if self._tuning:
            session.append("inner-loop tuning cleared")
        if self._last_result is not None:
            session.append("last measurement cleared")
        self._spec, self._tuning = None, {}
        self._last_result, self._last_metrics, self._bo_status = None, None, None
        self._last_diagnosis = None
        self._last_proposal = None
        released = [c for c in self.claims.list_claims() if c["owner"] == "agent"]
        for c in released:
            self.claims.release(c["device_id"], c["role"])
        if released:
            session.append("agent claims released: " + ", ".join(
                f"{c['device_id']} ({c['role']})" for c in released))
        if self._agent_rf:
            hardware.append("agent RF state dropped: " + ", ".join(sorted(self._agent_rf)))
        self._agent_rf = {}
        if self.backend.owns_channel:
            try:
                self.backend.set_condition(hop_plan=None)
                hardware.append("hop plan cleared")
                default_hz = None
                if self.topology is not None:
                    default_hz = (self.topology.common or {}).get("demo_center_freq_hz")
                if default_hz:
                    self.backend.set_condition(center_freq_hz=float(default_hz))
                    hardware.append(f"link returned to the declared channel {float(default_hz)/1e6:.3f} MHz")
                if self._channel:
                    self.backend.set_condition(**self._channel)
                    hardware.append("hidden channel condition re-applied")
            except Exception as exc:  # noqa: BLE001 - report, never block a switch on a radio
                hardware.append(f"radio reset incomplete: {exc}")
        if self.on_reset is not None:
            try:
                operator.extend(self.on_reset(reason) or [])
            except Exception as exc:  # noqa: BLE001
                operator.append(f"operator reset hook failed: {exc}")
        return {"reason": reason, "session": session, "hardware": hardware, "operator": operator}

    def _adopt(self, ledger: EditLedger) -> None:
        old = self.ledger
        self.ledger = ledger
        self._learned = LearnedSkillStore(ledger.path.parent / "learned_skills.jsonl")
        if old is not None and old is not ledger:
            try:
                old.store.close()
            except Exception:  # noqa: BLE001
                pass

    def start_experiment(self, name: str, goal: str = "") -> dict:
        """Create a NEW experiment and make it current. Resets the session and the radios."""
        try:
            ledger, info = self.manager.create(name, goal=goal, backend=self.experiment_backend)
        except ExperimentError as exc:
            raise ToolError("experiment_exists" if "already exists" in str(exc) else "bad_name", str(exc))
        reset = self._reset_session(f"started experiment {info.name!r}")
        self._adopt(ledger)
        _log.info("experiment started: %r -> %s | reset: %s", info.name, info.dir, _reset_line(reset))
        return {"ok": True, "action": "started", "experiment": info.to_dict(), "reset": reset}

    def switch_experiment(self, name: str) -> dict:
        """Reopen an EXISTING experiment and make it current. Resets the session and the radios."""
        if self.ledger is not None and self.ledger.path.parent.name == slugify(name):
            return {"ok": True, "action": "unchanged", "experiment": self.current_experiment(),
                    "reset": {"session": [], "hardware": [], "operator": []}}
        try:
            ledger, info = self.manager.open(name, backend=self.experiment_backend)
        except ExperimentError as exc:
            raise ToolError("unknown_experiment", str(exc))
        reset = self._reset_session(f"switched to experiment {info.name!r}")
        self._adopt(ledger)
        _log.info("experiment switched: %r (%d iterations) | reset: %s", info.name,
                  info.iterations, _reset_line(reset))
        return {"ok": True, "action": "switched", "experiment": info.to_dict(), "reset": reset}

    def rename_experiment(self, name: str, goal: str | None = None) -> dict:
        """Rename the CURRENT experiment (and its directory). Nothing is reset: the history and
        the built link carry on under the new name."""
        self._require_experiment()
        try:
            info = self.manager.rename(self.ledger, name, goal)
        except ExperimentError as exc:
            raise ToolError("bad_name", str(exc))
        # reopen at the new location; the store was closed for the move
        ledger, _ = self.manager.open(info.slug)
        self._adopt(ledger)
        _log.info("experiment renamed -> %r (%s)", info.name, info.dir)
        return {"ok": True, "action": "renamed", "experiment": info.to_dict()}

    def delete_experiment(self, name: str) -> dict:
        """Delete a SAVED experiment and everything under its directory. IRREVERSIBLE.

        Operator-only, and deliberately not a tool on the MCP surface: the agent's job is to
        measure and record, and nothing it does should be able to destroy a previous run's
        measurements. The console reaches this through POST /experiments, under the same bearer
        token as start/switch/rename.

        The CURRENT experiment is refused. Deleting the ledger the session is writing into would
        leave an open SQLite handle pointing at a directory that no longer exists, and every
        subsequent append would fail in a way that looks like a hardware fault. Switch away first;
        that path already closes the store and resets the session.
        """
        if self.ledger is not None and self.ledger.path.parent.name == slugify(name):
            raise ToolError("experiment_in_use",
                            "this is the current experiment -- switch to another one before "
                            "deleting it")
        try:
            info = self.manager.delete(name)
        except ExperimentError as exc:
            raise ToolError("unknown_experiment", str(exc))
        _log.warning("experiment DELETED: %r (%d iterations) from %s",
                     info.name, info.iterations, info.dir)
        return {"ok": True, "action": "deleted", "experiment": info.to_dict()}

    def finish_experiment(self, summary: str = "") -> dict:
        """Declare the experiment CONCLUDED. Call this exactly once, when all measuring is done.

        This is the only end-of-run signal in the system, and it exists because nothing else can
        produce one honestly. The console cannot tell a finished run from a thinking agent: both
        look like a ledger that has stopped growing, and a run that pauses to read a constellation
        or write an annotation is indistinguishable from one that has ended. Guessing from quiet
        is what made the report appear in the middle of a ladder -- repeatedly, since every
        subsequent trial re-armed the guess.

        So the agent says so. Ending the experiment stamps ``ended_at``, which the console watches
        and which fires the run report exactly once. Anything appended afterwards clears the stamp
        again, so a run that turns out to have more to measure simply carries on and is concluded
        again later.

        Returns what was concluded, so the report the agent writes and the report the console
        draws are built from the same numbers.
        """
        self._require_experiment()
        if summary:
            # The agent's own one-line verdict, kept beside the measurements it came from.
            self._record_decision(summary[:400], verdict="conclusion")
        outcome = self._run_outcome()
        ended_at = self.ledger.finish()
        slug = self.ledger.path.parent.name
        info = self.manager.info(slug)
        if ended_at is None:
            _log.info("experiment %r was already concluded", slug)
            return {"ok": True, "action": "already_finished", **outcome,
                    "experiment": info.to_dict() if info else {"slug": slug}}
        _log.info("experiment concluded: %r after %d iterations", slug, outcome["iterations"])
        return {"ok": True, "action": "finished", "ended_at": ended_at, **outcome,
                "report": "the console has raised this run's report",
                "experiment": info.to_dict() if info else {"slug": slug}}

    def _run_outcome(self) -> dict:
        """What this experiment measured, reduced to the few facts a conclusion rests on.

        Deliberately plain: counts, the best graded trial and the decisions that were taken. It
        does not decide what the run MEANT -- that is the agent's job, and a framework that
        guessed at it would be inventing a conclusion the measurements may not support.
        """
        rows = self.ledger.read()
        graded = [r for r in rows if isinstance((r.get("metrics") or {}).get("BER"), (int, float))]
        decisions = [{"iteration": r["iteration"], "what": r["edit_description"],
                      "verdict": r["verdict"]}
                     for r in rows if r.get("verdict") in _DECISION_VERDICTS]
        best = min(graded, key=lambda r: r["metrics"]["BER"], default=None)
        structures = list(dict.fromkeys(r["structure_id"] for r in graded if r["structure_id"]))
        return {
            "iterations": len(rows),
            "graded_trials": len(graded),
            "structures": structures,
            "current_structure_id": self._spec.structure_id if self._spec else None,
            "best_trial": ({"iteration": best["iteration"],
                            "structure_id": best["structure_id"],
                            "metrics": best["metrics"]} if best else None),
            "decisions": decisions,
        }

    # ---- topology guards ---------------------------------------------------

    def _require_verified_topology(self) -> None:
        """Refuse to measure on a bench whose declaration the hardware contradicts.

        The alternative -- running anyway and noting it somewhere -- is how a wrong bench produces
        confident, unpublishable numbers. The error names the disagreement so it can be fixed.
        """
        if self.topology_findings:
            raise ToolError(
                "topology_fault",
                "the hardware disagrees with the declared bench topology, so measurements would "
                "not describe the bench they claim to: " + "; ".join(
                    str(f) for f in self.topology_findings))

    def _check_in_band(self, freqs_hz) -> None:
        """Every frequency the LINK is asked to use must be reachable by both radios.

        Without this an agent can retune below the receiver's floor and get a confusing
        measurement failure instead of an honest refusal it can act on.
        """
        band = self.topology.link_band_hz if self.topology is not None else None
        if not band:
            return
        lo, hi = band
        for f in freqs_hz:
            if not (lo <= float(f) <= hi):
                raise ToolError(
                    "out_of_band",
                    f"{float(f)/1e6:.3f} MHz is outside the link band "
                    f"{lo/1e6:.0f}-{hi/1e6:.0f} MHz — the band both radios can reach")

    def _check_applies(self, key: str, what: str) -> None:
        """Refuse an agent RF action the backend cannot actually carry out.

        ``set_condition`` is deliberately tolerant -- a sim-style channel dict must not crash a
        radio -- so a knob the backend does not implement is dropped without a word. That is how
        ``set_hop_plan`` reported success for a whole Stage-3 experiment on a backend that never
        hopped: the plan was recorded, the ledger said "hopping", the following jammer chased the
        schedule, and the link sat on one channel the entire time. A measurement taken against a
        defence that never ran is worse than a missing measurement, because nothing about it looks
        wrong. Backends declare what they apply; anything else is an honest refusal.
        """
        applies = getattr(self.backend, "applies_condition", None)
        if not applies:
            return                      # backend declares nothing -- nothing to check against
        if key not in applies:
            raise ToolError(
                "not_supported",
                f"this backend ({getattr(self.backend, 'name', '?')}) cannot {what}: it applies "
                f"{sorted(applies)} and would silently ignore {key!r}")

    # ---- operator/framework only (NOT part of the agent tool surface) -------

    def set_channel(self, **channel) -> None:
        """Operator sets the hidden link condition. Not an agent-callable tool."""
        self._channel = dict(channel)

    def update_channel(self, **changes) -> dict:
        """Merge changes into the hidden condition, leaving the rest of it alone.

        The operator moves one knob at a time during a campaign -- step the transmit attenuation,
        hold everything else -- and :meth:`set_channel` replaces the whole condition, so using it
        for that silently drops whatever was already set. Returns the new condition to the
        OPERATOR only; it never reaches the agent's tool surface.
        """
        self._channel.update({k: v for k, v in changes.items() if v is not None})
        return dict(self._channel)

    def _channel_kwargs(self) -> dict:
        """Apply the hidden channel to the backend if it owns it (hardware sets radio gains),
        otherwise hand it back as LinkParams kwargs for build_and_run (sim). Either way the
        agent-facing tools never see the condition. The agent's own RF actions (retune / hop) are
        applied AFTER the hidden channel so they take effect on the run."""
        if self.backend.owns_channel:
            self.backend.set_condition(**self._channel)
            if self._agent_rf:
                self.backend.set_condition(**self._agent_rf)
            return {}
        return dict(self._channel)

    def _declared_center_freq_hz(self) -> float | None:
        """The channel the bench declares the link starts on, before the agent retunes anything."""
        if self.topology is None:
            return None
        try:
            v = (self.topology.common or {}).get("demo_center_freq_hz")
        except (AttributeError, TypeError):
            return None
        try:
            return float(v) if v else None
        except (TypeError, ValueError):
            return None

    def _link_freq_hz(self) -> float | None:
        """Where the link is tuned RIGHT NOW: the agent's retune if it made one, otherwise the
        channel the bench declared and the reset already tuned the radios to.

        The fallback is the point. ``_agent_rf`` is empty until the agent retunes, so a run that
        never needed to move -- or every run BEFORE the first move -- recorded no frequency at
        all, and a ledger that cannot say which channel a BER was taken on cannot show an
        interference episode: the baseline and the jammed trials read as the same channel, the
        first retune records no origin to have moved off, and the console cannot match a sweep's
        occupied row against the link's own channel to flag it.
        """
        return self._agent_rf.get("center_freq_hz") or self._declared_center_freq_hz()

    # ---- Discovery ---------------------------------------------------------

    def list_blocks(self, category=None, search=None) -> list:
        return self.blocks.list_blocks(category=category, search=search)

    def describe_block(self, name: str) -> dict:
        try:
            return self.blocks.describe_block(name)
        except KeyError:
            raise ToolError("unknown_block", f"no block named {name!r}")

    def list_skills(self) -> list:
        # curated primitives + the agent's own test-gated learned skills (spec C5)
        return self.skills.list_skills() + [{**s, "learned": True} for s in self._learned.list()]

    def describe_skill(self, name: str) -> dict:
        try:
            return self.skills.describe_skill(name)
        except KeyError:
            raise ToolError("unknown_skill", f"no skill named {name!r}")

    def list_devices(self) -> list:
        return [d.to_dict() for d in self.devices.values()]

    def probe_device(self, device_id: str) -> dict:
        if device_id not in self.devices:
            raise ToolError("unknown_device", f"no device {device_id!r}")
        dev = self.devices[device_id]
        return probe_device(device_id, backend=self.device_backend, uri=dev.uri).to_dict()

    def get_rf_path_config(self) -> dict:
        return dict(self._rf_path)

    def list_experiments(self) -> list:
        return [
            {"name": "link_only", "stage": 1, "doc": "clean two-node link under test"},
            {"name": "link_adaptive", "stage": 3,
             "doc": "two-node link under CHANGING conditions (SNR shifts, a jammer may appear or "
                    "follow) — hold BER and maximize efficiency by AMC, frequency avoidance, and hopping"},
        ]

    def describe_experiment(self, name: str) -> dict:
        if name == "link_only":
            return {"name": "link_only", "participants": ["transmitter", "receiver"],
                    "metrics": ["BER", "EVM", "SNR"],
                    "success": "BER <= target, maximize spectral efficiency"}
        if name == "link_adaptive":
            return {"name": "link_adaptive",
                    "participants": ["transmitter", "receiver", "interferer?"],
                    "metrics": ["BER", "EVM", "SNR"],
                    "success": "keep BER <= target and maximize spectral efficiency as conditions change",
                    "actions": ["build_flowgraph (AMC ladder)", "start_bo_run (tune a rung)",
                                "sense_spectrum (find a jammer)", "set_center_freq (avoid it)",
                                "set_hop_plan (out-hop a follower)"]}
        raise ToolError("unknown_experiment", f"no experiment {name!r} (try 'link_only', 'link_adaptive')")

    # ---- Construct (whole-graph-as-JSON, spec §8 revised) ------------------

    def validate(self, spec: dict) -> dict:
        res = validate_spec(FlowgraphSpec.from_dict(spec), self.skills)
        return {"ok": res.ok, "errors": res.errors, "warnings": res.warnings}

    def build_flowgraph(self, spec: dict) -> dict:
        self._require_experiment()
        fg = FlowgraphSpec.from_dict(spec)
        res = validate_spec(fg, self.skills)
        if not res.ok:
            raise ToolError("invalid_flowgraph", "; ".join(res.errors))
        self._spec = fg
        self._tuning = {}
        return {"ok": True, "structure_id": fg.structure_id, "modulation": fg.modulation}

    # ---- Claim -------------------------------------------------------------

    def claim_device(self, device_id: str, role: str) -> dict:
        self._require_experiment()
        try:
            c = self.claims.claim_device(device_id, role, owner="agent")
        except ClaimError as exc:
            raise ToolError("claim_denied", str(exc))
        return {"device_id": c.device_id, "role": c.role, "owner": c.owner}

    # ---- Execute -----------------------------------------------------------

    def run_flowgraph(self, n_bits: int | None = None) -> dict:
        self._require_experiment()
        self._require_verified_topology()
        if self._spec is None:
            raise ToolError("no_flowgraph", "build a flowgraph before running it")
        n = int(n_bits or self.confirm_bits)
        try:
            result = build_and_run(self._spec, self.backend, n_payload_bits=n,
                                   **self._channel_kwargs(), **self._tuning)
        except DeviceUnavailable as exc:
            # A radio went away. This is the ABSENCE of a measurement, not a bad one, and the
            # difference is the whole reason the exception is distinct.
            #
            # It must not be recorded as a result. A receiver that has stalled still hands back
            # buffers, and those grade as an error ratio near one half -- indistinguishable from a
            # jammed link. Writing that into the history would fabricate a measurement, and
            # telling the agent about it as a bad error ratio would teach it that the channel
            # degraded when in fact a cable came out; the reasonable response to that lie is to
            # drop the data rate, which fixes nothing.
            self.ledger.append(LedgerEntry(
                iteration=self.ledger.next_iteration(),
                structure_id=self._spec.structure_id,
                edit_description=f"run {self._spec.modulation}",
                metrics={},                      # deliberately empty: nothing was measured
                verdict="aborted:device_lost", loop="outer",
                note=str(exc)[:400]))
            self._last_result = self._last_metrics = self._last_diagnosis = None
            _log.error("run aborted, device lost: %s", exc)
            raise ToolError("device_unavailable", str(exc)) from exc
        metrics = compute_metrics(result.tx_bits, result.rx_bits, result.rx_syms, result.ref_syms)
        self._last_result, self._last_metrics = result, metrics
        self._last_diagnosis = None   # a new capture invalidates the previous read of it
        # Framework logs the iteration (spec §9).
        iteration = self.ledger.next_iteration()
        self.ledger.append(LedgerEntry(
            iteration=iteration, structure_id=self._spec.structure_id,
            edit_description=f"run {self._spec.modulation}",
            # The centre frequency travels with the measurement. Without it the ledger cannot say
            # which channel a BER was taken on -- and in a frequency-avoidance run that is half of
            # what happened: the same structure measured before and after a retune reads as two
            # identical rows with different numbers and no account of why.
            params={**{k: round(v, 4) for k, v in self._tuning.items()},
                    **({"center_freq_hz": float(_link_hz)}
                       if (_link_hz := self._link_freq_hz()) else {}),
                    **({"hop_channels_hz": [float(c) for c in self._agent_rf["hop_plan"].channels]}
                       if self._agent_rf.get("hop_plan") is not None else {})},
            # n_bits/n_errors travel with the ratio because a BER alone cannot be pooled or
            # bounded: 0 errors in 2e5 bits and 0 errors in 2e3 bits are both "BER 0", and a
            # capture that was cut short mid-run grades fewer bits than were asked for, which
            # is a different measurement from one that graded them all. Readers downstream
            # (the run report) need the counts to separate the two and to weight a pooled
            # ratio by bits rather than averaging ratios.
            metrics={"BER": metrics.ber, "EVM": round(metrics.evm_pct, 2),
                     "SNR_est": round(metrics.snr_db, 2),
                     "n_bits": metrics.n_bits, "n_errors": metrics.n_errors,
                     "n_bits_requested": n},
            verdict="run", loop="outer"))
        self._export_ran_flowgraph(iteration)
        return {"ran": True, "structure_id": self._spec.structure_id, "n_bits": metrics.n_bits}

    def _export_ran_flowgraph(self, iteration: int) -> None:
        """Keep a Companion file of every structure that produced a measurement.

        Without this an experiment's flowgraphs exist only if the agent remembered to call
        export_flowgraph, and a whole ladder of runs can end with nothing on the portal's
        Flowgraphs page to download. The file is written the first time a structure runs and
        recorded against that iteration; later runs of the same graph find it already there.

        An existing file is never overwritten. If ``<structure>.grc`` already holds a different
        graph -- the agent reused a name for a new structure, or the operator edited the file
        for import_flowgraph -- this graph gets a content-addressed name beside it instead, so
        every structure the ledger names stays downloadable and a hand edit is never lost.

        A failure here is logged and swallowed: the measurement has been taken and recorded,
        and losing its export must not turn a good run into an error.
        """
        d = self.artifacts_dir
        if d is None or self._spec is None:
            return
        try:
            from gr_autopilot.flowgraph.grc_export import render_grc

            text = render_grc(
                self._spec,
                freq_offset_hz=float(self._tuning.get("freq_offset_hz", 0.0) or 0.0),
                noise_amplitude=0.1,      # placeholder, as in export_flowgraph
            )
            stem = _grc_stem(self._spec.structure_id)
            target = d / f"{stem}.grc"
            if target.exists():
                if target.read_text(encoding="utf-8") == text:
                    return
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
                target = d / f"{stem}-{digest}.grc"
                if target.exists():
                    return
            d.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - an export must never fail a recorded run
            _log.warning("automatic flowgraph export failed for %r: %s",
                         self._spec.structure_id, exc)
            return
        try:
            self.ledger.store.add_artifact(
                self.ledger.experiment_id, kind="flowgraph.grc", path=target,
                iteration_seq=iteration,
                meta={"structure_id": self._spec.structure_id,
                      "modulation": self._spec.modulation,
                      "tx_chain": list(self._spec.tx_chain),
                      "rx_chain": list(self._spec.rx_chain),
                      "automatic": True})
        except Exception:  # noqa: BLE001 - recording provenance must never fail the run
            pass

    def get_status(self) -> dict:
        # Deliberately does NOT reveal the hidden channel (no SNR leak).
        return {
            "flowgraph_built": self._spec is not None,
            "structure_id": self._spec.structure_id if self._spec else None,
            "tuning": dict(self._tuning),
            "has_result": self._last_result is not None,
            "claims": self.claims.list_claims(),
        }

    def import_flowgraph(self, path: str) -> dict:
        """Load a GNU Radio Companion file and make it the flowgraph to run.

        This is the other half of exporting: the operator opens the exported graph in the editor,
        changes it, and hands it back. From then on the experiment runs THEIR graph.

        The file's content hash is recorded with the iteration, because a measurement produced by
        a hand-edited graph is not the same claim as one produced by the graph the agent proposed,
        and the two must be distinguishable afterwards.
        """
        self._require_experiment()
        from gr_autopilot.flowgraph.grc_import import UnsupportedFlowgraph, load_grc

        src = Path(path)
        if not src.exists():
            raise ToolError("not_found", f"no such flowgraph file: {src}")
        try:
            imported = load_grc(src)
        except UnsupportedFlowgraph as exc:
            raise ToolError("unsupported_flowgraph", str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise ToolError("import_failed", str(exc)) from exc

        res = validate_spec(imported.spec, self.skills)
        if not res.ok:
            raise ToolError("invalid_flowgraph",
                            "the imported graph is not runnable: " + "; ".join(res.errors))

        self._spec = imported.spec
        self._tuning = {}
        self.ledger.append(LedgerEntry(
            iteration=self.ledger.next_iteration(),
            structure_id=imported.spec.structure_id,
            edit_description=f"imported hand-edited flowgraph from {src.name}",
            params={"sha256": imported.sha256[:16]},
            verdict="imported", loop="outer",
            note=f"source={src} sha256={imported.sha256}"))
        try:
            self.ledger.store.add_artifact(
                self.ledger.experiment_id, kind="flowgraph.imported", path=src,
                iteration_seq=max(self.ledger.next_iteration() - 1, 0),
                meta={"sha256": imported.sha256, "overrides": imported.overrides,
                      "tx_chain": list(imported.spec.tx_chain),
                      "rx_chain": list(imported.spec.rx_chain)})
        except Exception:  # noqa: BLE001 - provenance must never block the import
            pass
        return {"ok": True, "structure_id": imported.spec.structure_id,
                "modulation": imported.spec.modulation,
                "tx_chain": list(imported.spec.tx_chain),
                "rx_chain": list(imported.spec.rx_chain),
                "settings_read_back": imported.overrides,
                "sha256": imported.sha256,
                "warnings": imported.warnings,
                "note": "subsequent runs use this graph, and are recorded as hand-edited"}

    def export_flowgraph(self, path: str | None = None) -> dict:
        """Write the current flowgraph as a GNU Radio Companion (``.grc``) file.

        Companion is the graphical editor shipped with GNU Radio; the exported file can be
        opened, read, edited and run there. On the gr-spec backend every block written
        corresponds to a block the experiment actually instantiates, with the same parameters;
        the Pluto backend executes a fixed receiver and honours only modulation, rolloff, sps and
        coding, so a hardware export records the requested structure (see grc_export.py).

        The channel's noise amplitude is deliberately NOT written: it is derived from the hidden
        channel quality, and putting it in a file the agent can read would disclose by arithmetic
        what the tool surface refuses to state.
        """
        self._require_experiment()
        if self._spec is None:
            raise ToolError("no_flowgraph", "build a flowgraph before exporting it")
        from gr_autopilot.flowgraph.grc_export import write_grc

        target = Path(path) if path else (
            self.artifacts_dir / f"{_grc_stem(self._spec.structure_id)}.grc")
        try:
            written = write_grc(
                self._spec, target,
                freq_offset_hz=float(self._tuning.get("freq_offset_hz", 0.0) or 0.0),
                noise_amplitude=0.1,      # placeholder: the true value would leak the condition
            )
        except (OSError, ValueError) as exc:
            raise ToolError("export_failed", str(exc)) from exc
        # Record it against the experiment, so the flowgraph and the iterations that produced it
        # stay associated rather than being a loose file someone has to match up by name.
        try:
            self.ledger.store.add_artifact(
                self.ledger.experiment_id, kind="flowgraph.grc", path=written,
                iteration_seq=max(self.ledger.next_iteration() - 1, 0),
                meta={"structure_id": self._spec.structure_id,
                      "modulation": self._spec.modulation,
                      "tx_chain": list(self._spec.tx_chain),
                      "rx_chain": list(self._spec.rx_chain)})
        except Exception:  # noqa: BLE001 - recording provenance must never fail the export
            pass
        return {
            "path": str(written.resolve()),
            "structure_id": self._spec.structure_id,
            "blocks": len(self._spec.tx_chain) + len(self._spec.rx_chain),
            "open_with": "gnuradio-companion",
            "note": ("Channel noise amplitude in the exported file is a placeholder, not the "
                     "experiment's hidden value."),
        }

    # ---- Measure (framework-owned) -----------------------------------------

    def _hop_records(self) -> dict | None:
        """Per-dwell grades for the last run, when it hopped. ``None`` otherwise.

        A hop cycle is graded as one population because that pooled ratio is what the link
        delivered. But pooled alone cannot distinguish interference spread thinly over every dwell
        from interference that landed whole on one of them, and telling those apart is the entire
        point of hopping. The backend records where each dwell's bits start and stop (its ACTUAL
        graded lengths, since a dwell whose frame was lost grades short); the grading itself stays
        here, in the framework, on the same code path as every other measurement.
        """
        r = self._last_result
        if r is None:
            return None
        meta = r.meta or {}
        counts = meta.get("dwell_bit_counts")
        if not counts:
            return None
        syms = list(meta.get("dwell_sym_counts") or [])
        chans = list(meta.get("hop_channels") or [])
        secs = list(meta.get("dwell_seconds") or [])
        rows, b0, s0 = [], 0, 0
        for i, n in enumerate(counts):
            ns = syms[i] if i < len(syms) else 0
            row = {"dwell": i,
                   "center_freq_hz": chans[i] if i < len(chans) else None,
                   "dwell_s": secs[i] if i < len(secs) else None,
                   "n_bits": int(n), "n_errors": None, "BER": None, "EVM": None, "SNR": None}
            if n > 0 and ns > 0:
                dm = compute_metrics(r.tx_bits[b0:b0 + n], r.rx_bits[b0:b0 + n],
                                     r.rx_syms[s0:s0 + ns], r.ref_syms[s0:s0 + ns])
                row.update(BER=dm.ber, EVM=dm.evm_pct, SNR=dm.snr_db,
                           n_bits=dm.n_bits, n_errors=dm.n_errors)
            rows.append(row)
            b0 += n; s0 += ns
        measured = [x for x in secs if x]
        return {"n_dwells": len(rows),
                # What the agent ASKED for versus what the hardware could do. A capture has a
                # floor (acquisition, DMA, settle) and a dwell cannot be shorter than one, so
                # these are two different numbers and a report that quotes only the request is
                # quoting a setting, not a measurement.
                "dwell_s_requested": meta.get("dwell_s_requested"),
                "dwell_s_measured_mean": (sum(measured) / len(measured)) if measured else None,
                "dwell_s_measured_min": min(measured) if measured else None,
                "dwell_s_measured_max": max(measured) if measured else None,
                "hop_rate_hz_requested": meta.get("hop_rate_hz"),
                "hop_rate_hz_measured": (len(measured) / sum(measured)) if measured else None,
                "dwells": rows}

    def get_metrics(self) -> dict:
        if self._last_metrics is None:
            raise ToolError("no_result", "run a flowgraph before reading metrics")
        m = self._last_metrics
        out = {"BER": m.ber, "EVM": m.evm_pct, "SNR": m.snr_db,
               "n_bits": m.n_bits, "n_errors": m.n_errors}
        hop = self._hop_records()
        if hop is not None:          # absent entirely for a non-hopped run
            out["hop"] = hop
        return out

    def capture_constellation(self, max_points: int = 256) -> dict:
        if self._last_result is None:
            raise ToolError("no_result", "run a flowgraph first")
        syms = self._last_result.rx_syms
        step = max(1, len(syms) // max_points)
        pts = syms[::step][:max_points]
        return {"modulation": self._last_result.modulation,
                "points": [[float(z.real), float(z.imag)] for z in pts],
                "evm_pct": self._last_metrics.evm_pct if self._last_metrics else None}

    def capture_spectrum(self) -> dict:
        # Artifacts-not-blobs: return a summary, not the full curve.
        if self._last_result is None:
            raise ToolError("no_result", "run a flowgraph first")
        import numpy as np
        x = self._last_result.rx_syms
        psd = np.abs(np.fft.fftshift(np.fft.fft(x[: 1 << 14]))) ** 2
        psd_db = 10 * np.log10(psd + 1e-12)
        return {"peak_db": float(psd_db.max()), "noise_floor_db": float(np.median(psd_db)),
                "n_bins": int(psd_db.size)}

    def diagnose_signal(self, render_path: str | None = None) -> dict:
        """Look at the last run's recovered constellation and name the fault + the fix — the agent
        *sees* the signal. Tells a phase rotation (tune the carrier) from low SNR (drop the rate) from
        a spinning ring (carrier unlocked), which a scalar BER cannot. Derived from the received
        symbols the agent already holds — never the hidden channel SNR. With ``render_path`` it also
        writes the scope PNG so a vision-language model client can read the same picture."""
        if self._last_result is None:
            raise ToolError("no_result", "run a flowgraph before diagnosing it")
        from gr_autopilot.perception.diagnose import diagnose_constellation
        syms = self._last_result.rx_syms
        # Score the constellation against the ACTUAL modulation's grid, not a hardcoded 16-QAM one —
        # else a clean BPSK/QPSK link reads as low-SNR and the agent is told to drop the rate it
        # should be climbing. The recovered symbols are the agent's; the hidden SNR is never used.
        mod = self._last_result.modulation or (self._spec.modulation if self._spec else "16qam")
        out = diagnose_constellation(syms, modulation=mod).to_dict()
        if render_path:
            from gr_autopilot.perception.render import constellation_png
            out["image_path"] = constellation_png(syms, render_path, title=f"{mod} constellation")
        # A diagnosis is something the agent SAW and acted on, so it belongs in the record beside
        # the measurement that prompted it. It also carries the only spur evidence the console
        # has between sweeps: `spur_offset_khz` / `spur_prominence_db` localise an interferer in
        # the received signal itself, where a sweep needs the link to go silent first.
        feats = out.get("features") or {}
        self._last_diagnosis = out
        self._record_decision(
            f"diagnose {out.get('fault', '?')}",
            verdict="diagnosed", structure_id=self._spec.structure_id if self._spec else "",
            params={"fault": out.get("fault"), "confidence": out.get("confidence"),
                    "modulation": mod,
                    **{k: feats[k] for k in ("spur_offset_khz", "spur_prominence_db",
                                             "rotation_deg", "grid_structure")
                       if k in feats}})
        return out

    # ---- Sense / retune / hop (Stage 2-3 agent actions) --------------------

    def _record_decision(self, edit: str, *, verdict: str, params: dict | None = None,
                         structure_id: str = "") -> None:
        """Write a row for something the agent DECIDED rather than measured.

        Retunes, hop plans and power changes used to leave no trace: the ledger held only graded
        runs, so a frequency-avoidance run read back as a handful of trials with no account of why
        the channel moved between them. The decisions are half of what happened, and a report that
        shows only the measurements cannot say what the run was for.

        These rows carry no metrics on purpose -- they are not measurements, and anything pooling
        error ratios must not find one here. Recorded quietly: failing to note a decision must
        never fail the decision itself.
        """
        if self.ledger is None:
            return
        try:
            self.ledger.append(LedgerEntry(
                iteration=self.ledger.next_iteration(), structure_id=structure_id,
                edit_description=edit, params=params or {}, metrics={},
                verdict=verdict, loop="outer"))
        except Exception:                                    # pragma: no cover - never fatal
            _log.debug("could not record decision %r", edit, exc_info=True)

    def sense_spectrum(self, freqs_hz: list, bw_hz: float | None = None) -> list:
        """Monitor role (spec §6): energy-detect received power over the noise floor at each
        candidate center frequency with the link silent. Locates a jammer so the agent can retune to
        a clear channel. A *reactive* jammer stays idle here (reads clear) — exactly the escalation
        energy detection cannot beat, which is why hopping exists. Never reveals the hidden SNR."""
        monitor = getattr(self.backend, "sense_spectrum", None)
        if monitor is None:
            raise ToolError("no_monitor", "this backend has no spectrum monitor")
        if not freqs_hz:
            raise ToolError("bad_request", "sense_spectrum needs at least one candidate frequency")
        if self.backend.owns_channel:      # apply the hidden channel so sensing sees the real jammer
            self.backend.set_condition(**self._channel)
        occ = monitor([float(f) for f in freqs_hz], bw_hz)
        busy = [o for o in occ if o.get("occupied")]
        # The sweep is the only EVIDENCE the console has that a channel is occupied, so the row
        # carries the detection threshold each row was judged against as well as the power. A
        # margin without its threshold cannot be read: +4 dB is occupied on the sim backend and
        # clear on the Plutos, and a dashboard that flags interference has to be able to tell.
        self._record_decision(
            f"sense {len(occ)} channel{'' if len(occ) == 1 else 's'} "
            f"({len(occ) - len(busy)} clear, {len(busy)} occupied)",
            verdict="sensed",
            params={"center_freq_hz": self._link_freq_hz(),
                    "occupancy": [{"center_freq_hz": o.get("center_freq_hz"),
                                   "power_db": o.get("power_db"),
                                   "peak_db": o.get("peak_db"),
                                   "detect_margin_db": o.get("detect_margin_db"),
                                   "occupied": bool(o.get("occupied"))} for o in occ]})
        return occ

    def current_link_freq(self):
        """Where the link is transmitting RIGHT NOW, hop plan included. Operator-side only.

        A channel-following jammer has to observe this, and observing only ``center_freq_hz``
        makes it blind to a hopping link: the centre frequency stops changing the moment a hop
        plan is set, so the follower parks and the Stage-3 experiment measures a stationary
        jammer while reporting that it was chasing. Returns ``None`` when nothing is tuned.

        The dwell is derived from the plan and the wall clock, which is what an adversary
        observing the air would see -- it does not privilege the follower with knowledge of the
        schedule, only with what is on the air at the instant it looks.
        """
        # What is ACTUALLY on the air, when the backend can say. A backend that physically hops
        # knows which channel the current dwell is on; the schedule arithmetic below is a model of
        # that, and a model is the wrong thing to hand a follower when the real answer exists.
        live = getattr(self.backend, "live_channel_hz", None)
        if live is not None:
            return float(live)
        plan = self._agent_rf.get("hop_plan")
        if plan is not None and plan.channels:
            import time
            idx = int(time.time() / plan.dwell_s) % len(plan.channels)
            return float(plan.channels[idx])
        return self._agent_rf.get("center_freq_hz")

    def set_center_freq(self, center_freq_hz: float) -> dict:
        """Agent action: retune the link center frequency (frequency avoidance). Persists across
        runs until changed. Requires a backend whose channel it owns (sim interference / radios)."""
        if not self.backend.owns_channel:
            raise ToolError("not_tunable", "this backend has no agent-controllable center frequency")
        self._check_applies("center_freq_hz", "retune the link")
        f = float(center_freq_hz)
        self._check_in_band([f])
        # Where we came FROM, which is the declared channel until the agent has moved once. A
        # retune that records no origin leaves the report printing "- -> 2.380 GHz": the move is
        # in the ledger but the channel it escaped is not.
        was = self._link_freq_hz()
        self._agent_rf["center_freq_hz"] = f
        self.backend.set_condition(center_freq_hz=f)
        self._record_decision(
            f"retune {f/1e6:.3f} MHz" + (f" (from {was/1e6:.3f} MHz)" if was else ""),
            verdict="retuned", params={"center_freq_hz": f,
                                       **({"from_hz": float(was)} if was else {})})
        return {"center_freq_hz": f, "previous_center_freq_hz": was}

    def set_hop_plan(self, channels_hz: list, hop_rate_hz: float) -> dict:
        """Agent action (Stage 3): hop the link across a channel set at ``hop_rate_hz`` to out-run a
        channel-following jammer. Raise the hop rate until BER meets; a fast enough follower still
        wins (the honest 1/τ floor). Persists until clear_hop_plan."""
        self._require_experiment()
        from gr_autopilot.link.hopping import HopPlan
        if not self.backend.owns_channel:
            raise ToolError("not_tunable", "this backend cannot hop")
        self._check_applies("hop_plan", "hop")
        self._check_in_band(channels_hz)
        if not channels_hz:
            raise ToolError("bad_hop_plan", "need at least one channel to hop")
        if float(hop_rate_hz) <= 0.0:
            raise ToolError("bad_hop_plan", "hop_rate_hz must be positive")
        hp = HopPlan(tuple(float(f) for f in channels_hz), float(hop_rate_hz))
        self._agent_rf["hop_plan"] = hp
        self.backend.set_condition(hop_plan=hp)
        # Belt and braces over the declaration above: read the plan back off the backend. A
        # decorator that was not wired in, or an inner backend that swallowed it, fails here
        # rather than at the end of an experiment that has already been believed.
        if getattr(self.backend, "hop_plan", None) is not hp:
            self._agent_rf.pop("hop_plan", None)
            raise ToolError(
                "not_supported",
                f"this backend ({getattr(self.backend, 'name', '?')}) accepted a hop plan but did "
                f"not keep it -- the link would not have hopped")
        self._record_decision(
            f"hop {len(hp.channels)} channels at {hp.hop_rate_hz:.0f} Hz",
            verdict="hopping", params={"hop_rate_hz": hp.hop_rate_hz,
                                       "channels_hz": list(hp.channels)})
        return {"channels_hz": list(hp.channels), "hop_rate_hz": hp.hop_rate_hz,
                "dwell_s": round(hp.dwell_s, 6)}

    def clear_hop_plan(self) -> dict:
        """Agent action: stop hopping and sit on the current center frequency (threat gone)."""
        had = self._agent_rf.pop("hop_plan", None) is not None
        if self.backend.owns_channel:
            self.backend.set_condition(hop_plan=None)
        if had:
            self._record_decision("stop hopping", verdict="hopping_cleared")
        return {"hopping": False, "was_hopping": had}

    def set_tx_power(self, power_db: float) -> dict:
        """Agent action (power adaptation): set TX power (dB, relative) within the power budget. Back
        off to transmit no louder than needed (efficiency / low interference footprint), or boost to
        hold a higher modulation or overcome a jammer. Clamped to the budget (a finite PA ceiling); the
        path loss stays hidden, so the agent finds the power it needs by measurement."""
        if not self.backend.owns_channel:
            raise ToolError("not_tunable", "this backend has no agent-controllable TX power")
        self._check_applies("tx_power_db", "set transmit power")
        lo, hi = TX_POWER_BUDGET_DB
        p = max(lo, min(hi, float(power_db)))
        was = self._agent_rf.get("tx_power_db")
        self._agent_rf["tx_power_db"] = p
        self.backend.set_condition(tx_power_db=p)
        self._record_decision(f"tx power {p:+.1f} dB", verdict="power",
                              params={"tx_power_db": p,
                                      **({"from_db": float(was)} if was is not None else {})})
        return {"tx_power_db": p, "clamped": p != float(power_db), "budget_db": [lo, hi]}

    # ---- Learn (author + promote skills — self-improvement, spec C5) --------

    def author_skill(self, name: str, ladder: list | None = None, target_ber: float = 1e-2) -> dict:
        """Experiment on the grader at the CURRENT channel to discover a new building block: benchmark a
        ladder of candidate modcods (including composite coded rungs) and, if a coded rung fills an
        efficiency gap the base rungs cannot, propose it as a learned skill. Does NOT promote it yet
        (see promote_skill). All measurement is the framework grader; the agent never scores its own."""
        self._require_experiment()
        if name in self._learned or self.skills.get(name) is not None:
            raise ToolError("name_taken", f"skill {name!r} already exists")
        self._last_author_target = float(target_ber)
        author = SkillAuthor(self.backend, target_ber=float(target_ber),
                             channel_kwargs=self._channel_kwargs())
        ladder = ladder or ["bpsk", "qpsk", "16qam", "qpsk:conv_k3_r34", "16qam:conv_k3_r34"]
        prop = author.author(name, list(ladder))
        self._last_proposal = prop
        if prop is None:
            # Say WHICH rungs met and which did not. "Nothing beat the base rungs" is not
            # actionable: a coded rung can only win in the window where the uncoded rung above it
            # is failing and the one below it is wasting efficiency, so an empty result usually
            # means the channel is on the wrong side of that window, not that coding never helps.
            bench = author.last_bench
            met = [mc for mc, v in bench.items() if v["meets"]]
            failed = [mc for mc, v in bench.items() if not v["meets"]]
            if not met:
                why = ("no rung met the target at this channel — the link is too poor for any of "
                       "them; lower the target or improve the channel")
            elif not any(":" in mc for mc in failed):
                why = ("an uncoded rung already meets the target at the top of the ladder, so "
                       "coding would only cost efficiency; try a WEAKER channel, where the "
                       "uncoded rung above the current best fails")
            else:
                why = ("the coded rungs did not meet the target either — this channel is below "
                       "the window where coding gain closes the gap; try a stronger channel")
            return {"authored": False, "reason": why, "ladder": bench,
                    "met": met, "failed": failed, "target_ber": float(target_ber)}
        return {"authored": True, "name": name, "benchmark": prop.benchmark, "spec": prop.spec,
                "ladder": author.last_bench}

    def promote_skill(self, name: str | None = None, min_gain: float = 0.4) -> dict:
        """Test-gate the last authored proposal and, on pass, PERSIST it into the agent's vocabulary: it
        must re-validate on a fresh grader trial and beat its baseline efficiency by ``min_gain``."""
        self._require_experiment()
        prop = self._last_proposal
        if prop is None or (name is not None and prop.name != name):
            raise ToolError("no_proposal", "author a skill first (author_skill)")
        author = SkillAuthor(self.backend, target_ber=self._last_author_target,
                             channel_kwargs=self._channel_kwargs())
        gate = author.promote(prop, self._learned, min_gain=float(min_gain))
        if gate.ok:
            self._last_proposal = None
        return {"promoted": gate.ok, "reason": gate.reason,
                "vocabulary": self._learned.names(), "n_learned": len(self._learned)}

    # ---- Optimize (BO inner loop) ------------------------------------------

    def start_bo_run(self, params: list, bounds: list, budget: int = 12,
                     target_ber: float | None = None, trial_bits: int = 40_000,
                     repeats: int = 3) -> dict:
        """Tune continuous knobs on the built structure.

        Each trial is the MEAN of ``repeats`` measurements, and the winner is re-measured before it
        is reported. Both exist because a single measurement is noisy enough to be optimised
        against instead of the knob: with one run per trial the search returns whichever trial got
        lucky, so the reported best does not reproduce. Measured on the bench -- a run reporting
        best loss 0.0069 re-measured at 2.3e-2, and the same point came back across independent
        searches because the initial design is deterministic.

        ``validated`` in the reply is the honest number: the best point, measured again on fresh
        data. When it is much worse than ``best_loss``, the search fit noise and the spread across
        repeats says how much noise there was to fit.
        """
        self._require_experiment()
        if self._spec is None:
            raise ToolError("no_flowgraph", "build a flowgraph before optimizing it")
        for p in params:
            if p not in TUNABLE_KNOBS:
                raise ToolError("untunable_param", f"{p!r} is not a tunable knob {list(TUNABLE_KNOBS)}")
        if int(repeats) < 1:
            raise ToolError("bad_repeats", "repeats must be at least 1")
        spec = self._spec
        n_rep = int(repeats)
        spreads = []

        def _measure(x):
            """One point, ``repeats`` times -> (mean loss, spread)."""
            kv = dict(zip(params, x))
            losses = []
            for _ in range(n_rep):
                r = build_and_run(spec, self.backend, n_payload_bits=trial_bits,
                                  **self._channel_kwargs(), **kv)
                losses.append(bo_loss(compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)))
            mean = sum(losses) / len(losses)
            return mean, (max(losses) - min(losses))

        def objective(x):
            mean, spread = _measure(x)
            spreads.append(spread)
            return mean

        def early(status):
            # LLM interrupt: stop if the target is already comfortably met (spec §4.3).
            if target_ber is not None and status.best_y <= target_ber:
                return True, f"target BER {target_ber:g} reached"
            return False, ""

        status = run_bo(objective, bounds, budget=budget, early_stop=early)
        self._tuning = dict(zip(params, status.best_x))  # persist tuning into the structure
        self._bo_status = status
        validated, val_spread = _measure(status.best_x)
        typical = (sum(spreads) / len(spreads)) if spreads else 0.0
        return {"trials": status.trials, "best_params": self._tuning,
                "best_loss": status.best_y,
                "validated_loss": validated,
                "validated_spread": val_spread,
                "typical_trial_spread": typical,
                # A best that does not survive re-measurement, by more than the noise the search
                # was working through, is a fit to that noise rather than to the knob.
                "reproduced": bool(validated <= status.best_y + max(typical, 1e-12)),
                "repeats_per_trial": n_rep,
                "stopped_early": status.stopped_early,
                "stop_reason": status.stop_reason}

    def get_bo_status(self) -> dict:
        if self._bo_status is None:
            raise ToolError("no_bo_run", "no BO run has been started")
        s = self._bo_status
        return {"trials": s.trials, "best_params": dict(self._tuning),
                "best_loss": s.best_y, "stopped_early": s.stopped_early}

    def stop_bo_run(self) -> dict:
        # Runs are synchronous here; expose the interrupt via start_bo_run(target_ber=...).
        return {"stopped": True}

    # ---- Memory ------------------------------------------------------------

    def read_edit_ledger(self, compact: bool = True):
        self._require_experiment()
        return self.ledger.compact_table() if compact else self.ledger.read()

    def annotate_ledger(self, iteration: int, note: str) -> dict:
        self._require_experiment()
        self.ledger.annotate(iteration, note)
        return {"ok": True}
