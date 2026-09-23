"""The record of what was tried and what it measured.

One entry per iteration of the outer (structural) or inner (parameter tuning) loop. The ledger
is also rendered back to the model as a compact table each turn, which is how it remembers what
it has already attempted.

Storage is a local SQLite database (see :mod:`gr_autopilot.ledger.store`), grouped into
experiments. This class is the narrow, familiar interface onto it: append an iteration, read
them back, render a table. Anything that needs experiments as first-class objects -- listing
them, recording artifacts against them -- should use :class:`~gr_autopilot.ledger.store.ExperimentStore`
directly.

A path ending in ``.jsonl`` is accepted and mapped to a sibling ``.db`` file, so the many call
sites written against the previous file-based ledger continue to work unchanged.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gr_autopilot.ledger.store import ExperimentStore


@dataclass
class LedgerEntry:
    """One recorded iteration (spec §9)."""

    iteration: int
    structure_id: str            # which flowgraph structure (e.g. "qpsk_v2")
    edit_description: str         # what changed ("switched QPSK->16QAM")
    params: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)   # BER / EVM / SNR_est ...
    verdict: str = ""             # kept | reverted | tuning | baseline ...
    loop: str = "outer"           # outer (LLM structural) | inner (BO trial)
    note: str = ""                # free-text annotation

    def to_dict(self) -> dict:
        return asdict(self)


def db_path_for(path: str | os.PathLike) -> Path:
    """The database file for a given ledger path.

    A ``.jsonl`` path maps to a sibling ``.db``: the call sites predate the database and pass
    names like ``session.jsonl``, and quietly writing a database to a file named for a text
    format would be confusing to anyone who went looking for it.
    """
    p = Path(path)
    return p.with_suffix(".db") if p.suffix in (".jsonl", ".json", ".log") else p


class EditLedger:
    """Iteration history for one experiment, backed by SQLite."""

    def __init__(self, path: str | os.PathLike, *, experiment: str = "session",
                 goal: str = "", backend: str = "", experiment_id: int | None = None):
        self.path = db_path_for(path)
        self.store = ExperimentStore(self.path)
        if experiment_id is None:
            # Reuse the most recent experiment in this database so that constructing a ledger
            # twice against the same path continues one history rather than starting a parallel
            # one -- the operator console does exactly that on reset, and switch_experiment does
            # it on every reopen. A CONCLUDED experiment is reused too: ending a run marks that
            # the agent finished reporting on it, not that the record is sealed, and the first
            # append reopens it (see ExperimentStore.add_iteration). Were it skipped here, going
            # back to a finished experiment would fork a second history inside its own database.
            recent = self.store.list_experiments(limit=1)
            experiment_id = (recent[0].id if recent else
                             self.store.start_experiment(experiment, goal=goal, backend=backend))
        self.experiment_id = int(experiment_id)

    # ---- write -------------------------------------------------------------

    def append(self, entry: LedgerEntry | dict) -> None:
        record = entry.to_dict() if isinstance(entry, LedgerEntry) else dict(entry)
        self.store.add_iteration(
            self.experiment_id,
            seq=record.get("iteration"),
            loop=str(record.get("loop", "outer") or "outer"),
            structure_id=str(record.get("structure_id", "") or ""),
            edit=str(record.get("edit_description", "") or ""),
            verdict=str(record.get("verdict", "") or ""),
            note=str(record.get("note", "") or ""),
            params=record.get("params") or {},
            metrics=record.get("metrics") or {},
        )

    def finish(self) -> float | None:
        """Declare this experiment concluded; returns when, or None if it already was.

        This is the end-of-run signal the console waits for. Nothing infers it: a run ends when
        the agent says it has ended, because only the agent knows whether the next measurement is
        coming. Appending anything afterwards reopens the experiment, so a run that turns out not
        to be over can be concluded again later and will raise a second, later report.
        """
        return self.store.end_experiment(self.experiment_id)

    def annotate(self, iteration: int, note: str) -> None:
        """Record a note against a prior iteration."""
        self.append(
            LedgerEntry(
                iteration=iteration,
                structure_id="",
                edit_description="(annotation)",
                verdict="annotation",
                note=note,
            )
        )

    # ---- read --------------------------------------------------------------

    def read(self) -> list[dict]:
        return self.store.iterations(self.experiment_id)

    def __len__(self) -> int:
        return self.store.count_iterations(self.experiment_id)

    def next_iteration(self) -> int:
        return self.store.next_seq(self.experiment_id)

    # ---- render ------------------------------------------------------------

    def compact_table(self, max_rows: int | None = None) -> str:
        """Render the ledger as a compact fixed-width table for the LLM's context."""
        rows = self.read()
        if max_rows is not None:
            rows = rows[-max_rows:]
        if not rows:
            return "(edit ledger empty)"

        header = ["it", "loop", "structure", "edit", "chan/MHz", "BER", "EVM%", "SNR", "verdict"]
        lines = [header]
        for r in rows:
            m = r.get("metrics", {}) or {}
            lines.append([
                str(r.get("iteration", "")),
                str(r.get("loop", "")),
                str(r.get("structure_id", ""))[:14],
                str(r.get("edit_description", ""))[:28],
                _chan(r, m),
                _fmt(m.get("BER", m.get("ber"))),
                _fmt(m.get("EVM", m.get("evm_pct")), pct=True),
                _fmt(m.get("SNR_est", m.get("snr_db")), unit=""),
                str(r.get("verdict", ""))[:10],
            ])
        widths = [max(len(row[i]) for row in lines) for i in range(len(header))]
        return "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in lines)


def _chan(row: dict, metrics: dict) -> str:
    """The channel a MEASUREMENT was taken on, blank for everything else.

    Only a graded link run gets a frequency. A retune, a sweep or a diagnosis already says what
    it did in its own edit description, and stamping those rows too would put a number in the
    column that is not the channel any BER beside it was measured on. A hopping run has no single
    centre to name, so it says so rather than naming the one it happened to start from.
    """
    if metrics.get("BER") is None and metrics.get("ber") is None:
        return ""
    params = row.get("params") or {}
    if params.get("hop_channels_hz"):
        return f"hop x{len(params['hop_channels_hz'])}"
    hz = params.get("center_freq_hz")
    if hz is None:
        return "-"
    try:
        return f"{float(hz) / 1e6:.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "-"


def _fmt(value, pct: bool = False, unit: str = "") -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if pct:
        return f"{v:.1f}"
    if abs(v) < 1e-3 and v != 0:
        return f"{v:.1e}"
    return f"{v:.3g}{unit}"


def _json_default(obj):
    # Best-effort serialization for numpy scalars etc. without importing numpy here.
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)
