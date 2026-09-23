"""Experiment and iteration records, stored in a local SQLite database.

The project previously recorded every iteration as one line of JavaScript Object Notation in a
single append-only file. That file had no notion of an EXPERIMENT: iteration numbers simply
climbed, so a file accumulated over many sessions and where one run ended and the next began was
not recoverable from the data. It also had to be re-read in full to answer any question,
including "what is the next iteration number", which made appending cost time proportional to
everything written so far.

The model here is two levels deep, matching how the work is actually done:

    experiment          one investigation: a goal, a backend, a start and end time
      +-- iteration     one measured step within it: what changed, what was measured
      +-- artifact      a file the experiment produced, such as an exported flowgraph

SQLite is part of the Python standard library, so this adds no dependency, and the database is a
single local file that can be copied or deleted like any other.

Concurrency. The write-ahead log is enabled so a reader (the dashboard polling for the current
state) never blocks a writer (the measurement loop appending an iteration). A busy timeout is set
because more than one process may legitimately hold the database open -- previously each process
simply appended to the file and the operating system serialized it. Connections are created per
thread: the sqlite3 module on this interpreter reports thread safety level 1, meaning a single
connection must not be shared between threads.

Durability differs in kind from the file it replaces, and the difference is worth stating. The
old format promised a "truncated but parseable" log: a crash mid-write lost at most the final
line. A database gives something stronger for completed writes -- each is atomic, so a half-written
iteration cannot exist -- but a corrupted database file is not partially readable the way a
truncated text file is. The exchange is deliberate.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    goal        TEXT NOT NULL DEFAULT '',
    backend     TEXT NOT NULL DEFAULT '',
    started_at  REAL NOT NULL,
    ended_at    REAL,
    config_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS iteration (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    loop          TEXT NOT NULL DEFAULT 'outer',
    structure_id  TEXT NOT NULL DEFAULT '',
    edit          TEXT NOT NULL DEFAULT '',
    verdict       TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    params_json   TEXT NOT NULL DEFAULT '{}',
    metrics_json  TEXT NOT NULL DEFAULT '{}',
    ber           REAL,
    evm_pct       REAL,
    snr_db        REAL
    -- seq is deliberately NOT unique. An annotation is recorded as its own row carrying the
    -- sequence number of the iteration it refers to, which is how the previous format worked and
    -- how both the dashboard and the table rendered for the model expect to read it. Making the
    -- pair unique would silently overwrite an iteration when someone annotated it.
);

CREATE INDEX IF NOT EXISTS iteration_by_experiment ON iteration(experiment_id, seq);

CREATE TABLE IF NOT EXISTS artifact (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
    iteration_seq INTEGER,
    kind          TEXT NOT NULL,
    path          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    meta_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS artifact_by_experiment ON artifact(experiment_id);
"""

# BER, EVM and SNR are lifted out of the metrics document into their own columns so the common
# questions ("best error ratio in this experiment", "did it meet the target") are one query
# rather than a scan that parses every row. Everything else stays in the document.
_METRIC_COLUMNS = {
    "ber": ("BER", "ber"),
    "evm_pct": ("EVM", "evm_pct", "EVM%"),
    "snr_db": ("SNR_est", "snr_db", "SNR"),
}


def _extract_metric(metrics: dict, keys) -> float | None:
    for key in keys:
        if key in metrics:
            try:
                return float(metrics[key])
            except (TypeError, ValueError):
                return None
    return None


def _dumps(obj) -> str:
    def default(o):
        return o.item() if hasattr(o, "item") else str(o)
    return json.dumps(obj or {}, default=default)


@dataclass
class Experiment:
    id: int
    name: str
    goal: str
    backend: str
    started_at: float
    ended_at: float | None
    config: dict

    @property
    def running(self) -> bool:
        return self.ended_at is None


class ExperimentStore:
    """SQLite-backed record of experiments, their iterations, and their artifacts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._write_lock:
            conn = self._conn()
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
            conn.commit()

    # -- connection management ----------------------------------------------
    _write_lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        """One connection per thread: this interpreter's sqlite3 reports thread safety level 1,
        so a connection must not be shared across threads."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")     # readers never block the writer
            conn.execute("PRAGMA busy_timeout=15000")   # several processes may hold this open
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- experiments ---------------------------------------------------------
    def start_experiment(self, name: str, goal: str = "", backend: str = "",
                         config: dict | None = None) -> int:
        conn = self._conn()
        with self._write_lock:
            cur = conn.execute(
                "INSERT INTO experiment(name, goal, backend, started_at, config_json) "
                "VALUES (?,?,?,?,?)",
                (name, goal, backend, time.time(), _dumps(config)))
            conn.commit()
        return int(cur.lastrowid)

    def rename_experiment(self, experiment_id: int, name: str, goal: str | None = None) -> None:
        """Change what an experiment is called (and optionally its goal). Its iterations,
        artifacts and directory are untouched by this call -- moving the directory is the
        manager's job, because the store must not know where it lives on disk."""
        conn = self._conn()
        with self._write_lock:
            if goal is None:
                conn.execute("UPDATE experiment SET name=? WHERE id=?", (name, experiment_id))
            else:
                conn.execute("UPDATE experiment SET name=?, goal=? WHERE id=?",
                             (name, goal, experiment_id))
            conn.commit()

    def end_experiment(self, experiment_id: int) -> float | None:
        """Mark an experiment concluded, and say when.

        Returns the moment it was closed, or ``None`` if it was already closed -- the caller needs
        to tell "I just ended this" from "this was already ended", because concluding a run is the
        signal that raises its report and must therefore happen exactly once per run.
        """
        now = time.time()
        conn = self._conn()
        with self._write_lock:
            cur = conn.execute("UPDATE experiment SET ended_at=? WHERE id=? AND ended_at IS NULL",
                               (now, experiment_id))
            conn.commit()
        return now if cur.rowcount else None

    def get_experiment(self, experiment_id: int) -> Experiment | None:
        row = self._conn().execute("SELECT * FROM experiment WHERE id=?",
                                   (experiment_id,)).fetchone()
        return self._to_experiment(row) if row else None

    def list_experiments(self, limit: int = 50) -> list[Experiment]:
        rows = self._conn().execute(
            "SELECT * FROM experiment ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._to_experiment(r) for r in rows]

    @staticmethod
    def _to_experiment(row) -> Experiment:
        return Experiment(id=row["id"], name=row["name"], goal=row["goal"],
                          backend=row["backend"], started_at=row["started_at"],
                          ended_at=row["ended_at"], config=json.loads(row["config_json"] or "{}"))

    # -- iterations ----------------------------------------------------------
    def next_seq(self, experiment_id: int) -> int:
        """The next iteration number, from an indexed lookup rather than a full re-read."""
        row = self._conn().execute("SELECT MAX(seq) AS m FROM iteration WHERE experiment_id=?",
                                   (experiment_id,)).fetchone()
        return int((row["m"] or 0) + 1)

    def add_iteration(self, experiment_id: int, *, seq: int | None = None, loop: str = "outer",
                      structure_id: str = "", edit: str = "", verdict: str = "", note: str = "",
                      params: dict | None = None, metrics: dict | None = None) -> int:
        metrics = metrics or {}
        conn = self._conn()
        with self._write_lock:
            n = self.next_seq(experiment_id) if seq is None else int(seq)
            values = (experiment_id, n, time.time(), loop, structure_id, edit, verdict, note,
                      _dumps(params), _dumps(metrics),
                      _extract_metric(metrics, _METRIC_COLUMNS["ber"]),
                      _extract_metric(metrics, _METRIC_COLUMNS["evm_pct"]),
                      _extract_metric(metrics, _METRIC_COLUMNS["snr_db"]))
            conn.execute(
                "INSERT INTO iteration(experiment_id, seq, created_at, loop, "
                "structure_id, edit, verdict, note, params_json, metrics_json, ber, evm_pct, "
                "snr_db) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            # Activity reopens a concluded experiment. ``ended_at`` means "the agent declared this
            # run finished", and anything that appends afterwards makes that false again -- which
            # is what keeps the marker usable as a one-shot end-of-run signal rather than a
            # timestamp that drifts out of step with the history it is supposed to close.
            conn.execute("UPDATE experiment SET ended_at=NULL "
                         "WHERE id=? AND ended_at IS NOT NULL", (experiment_id,))
            conn.commit()
        return n

    def iterations(self, experiment_id: int, limit: int | None = None) -> list[dict]:
        # Ordered by insertion (id), not by seq: an annotation carries the sequence number of the
        # iteration it refers to, so ordering by seq would move it next to that iteration instead
        # of leaving it where it was written.
        sql = "SELECT * FROM iteration WHERE experiment_id=? ORDER BY id"
        args: tuple = (experiment_id,)
        if limit is not None:
            # Take the most recent rows, then restore chronological order for display.
            sql = ("SELECT * FROM (SELECT * FROM iteration WHERE experiment_id=? "
                   "ORDER BY id DESC LIMIT ?) ORDER BY id")
            args = (experiment_id, int(limit))
        return [self._to_row(r) for r in self._conn().execute(sql, args).fetchall()]

    @staticmethod
    def _to_row(r) -> dict:
        """Shaped like the record the previous format wrote, so existing readers -- the
        dashboard, the compact table rendered for the model -- need no changes."""
        return {
            "iteration": r["seq"],
            "loop": r["loop"],
            "structure_id": r["structure_id"],
            "edit_description": r["edit"],
            "verdict": r["verdict"],
            "note": r["note"],
            "params": json.loads(r["params_json"] or "{}"),
            "metrics": json.loads(r["metrics_json"] or "{}"),
            "created_at": r["created_at"],
        }

    def count_iterations(self, experiment_id: int) -> int:
        row = self._conn().execute("SELECT COUNT(*) AS c FROM iteration WHERE experiment_id=?",
                                   (experiment_id,)).fetchone()
        return int(row["c"])

    def best(self, experiment_id: int) -> dict | None:
        """The lowest recorded bit error ratio in an experiment -- one indexed query."""
        row = self._conn().execute(
            "SELECT * FROM iteration WHERE experiment_id=? AND ber IS NOT NULL "
            "ORDER BY ber LIMIT 1", (experiment_id,)).fetchone()
        return self._to_row(row) if row else None

    # -- artifacts -----------------------------------------------------------
    def add_artifact(self, experiment_id: int, kind: str, path: str | Path,
                     iteration_seq: int | None = None, meta: dict | None = None) -> int:
        conn = self._conn()
        with self._write_lock:
            cur = conn.execute(
                "INSERT INTO artifact(experiment_id, iteration_seq, kind, path, created_at, "
                "meta_json) VALUES (?,?,?,?,?,?)",
                (experiment_id, iteration_seq, kind, str(path), time.time(), _dumps(meta)))
            conn.commit()
        return int(cur.lastrowid)

    def artifacts(self, experiment_id: int, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM artifact WHERE experiment_id=?"
        args: tuple = (experiment_id,)
        if kind:
            sql += " AND kind=?"
            args += (kind,)
        sql += " ORDER BY created_at"
        return [{"id": r["id"], "kind": r["kind"], "path": r["path"],
                 "iteration_seq": r["iteration_seq"], "created_at": r["created_at"],
                 "meta": json.loads(r["meta_json"] or "{}")}
                for r in self._conn().execute(sql, args).fetchall()]


def import_jsonl(store: ExperimentStore, path: str | Path, name: str = "imported-session") -> int:
    """Load a legacy JavaScript Object Notation log into the database as ONE experiment.

    The old format recorded no run boundaries and no timestamps, so an accumulated file cannot be
    split back into the separate sessions that produced it. Importing it as a single experiment
    is the only honest reading of the data; inventing boundaries would fabricate history. The
    original iteration numbers are preserved as the sequence.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    experiment_id = store.start_experiment(
        name=name, goal="imported from a pre-database log", backend="unknown",
        config={"source": str(path),
                "note": "the source format recorded no run boundaries; this is one experiment "
                        "only because the original divisions are not present in the data"})
    seq = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:      # the old format tolerated a truncated final line
            continue
        seq += 1
        store.add_iteration(
            experiment_id, seq=seq, loop=row.get("loop", "outer"),
            structure_id=str(row.get("structure_id", "")),
            edit=str(row.get("edit_description", "")), verdict=str(row.get("verdict", "")),
            note=str(row.get("note", "")), params=row.get("params") or {},
            metrics=row.get("metrics") or {})
    store.end_experiment(experiment_id)
    return experiment_id
