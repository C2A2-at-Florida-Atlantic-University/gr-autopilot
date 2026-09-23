"""Experiments as things you name, open, switch between and rename -- not a daemon flag.

Until now the experiment was fixed at launch: ``--ledger runs/<name>/session.jsonl`` decided
where everything went, and changing it meant restarting the server. That does not survive a
working session in which an operator wants to run two or three separate investigations, and it
put the name in the one place neither the dashboard nor the agent could reach.

This module owns the layout instead. A *runs directory* holds one subdirectory per experiment,
named by a filesystem-safe slug of the experiment's display name::

    runs/
      amc-ladder-2370/
        session.db            iterations, artifacts, the experiment row (name, goal, backend)
        telemetry.json        the last measurement, for the dashboard
        flowgraphs/           exported .grc files
        learned_skills.jsonl  the agent's promoted vocabulary

Nothing here touches radios or session state; :class:`AutopilotService` does that when it
switches, because only it knows what a switch must clear.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from gr_autopilot.ledger.ledger import EditLedger
from gr_autopilot.ledger.store import ExperimentStore

DB_NAME = "session.db"
SNAPSHOT_NAME = "telemetry.json"
FLOWGRAPHS_DIR = "flowgraphs"
LEARNED_NAME = "learned_skills.jsonl"

_SLUG_OK = re.compile(r"[^a-z0-9._-]+")


class ExperimentError(ValueError):
    """A name that cannot be used, or an experiment that is not there / already there."""


def slugify(name: str) -> str:
    """The directory name for a display name: lower case, spaces to hyphens, only ``[a-z0-9._-]``.

    Rejects the empty string and anything that would escape the runs directory. Kept simple on
    purpose -- it has to be typeable into a shell and stable across operating systems.
    """
    s = _SLUG_OK.sub("-", (name or "").strip().lower()).strip("-.")
    s = re.sub(r"-{2,}", "-", s)
    if not s or s in {".", ".."} or "/" in s or "\\" in s:
        raise ExperimentError(f"{name!r} cannot be used as an experiment name")
    if len(s) > 80:
        raise ExperimentError("experiment name is too long (80 characters after slugging)")
    return s


@dataclass
class ExperimentInfo:
    name: str
    slug: str
    dir: str
    goal: str
    backend: str
    started_at: float | None
    ended_at: float | None
    iterations: int
    last_activity: float | None   # newest iteration or file change, for ordering a picker

    @property
    def running(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["running"] = self.running
        return d


class ExperimentManager:
    def __init__(self, runs_dir: str | os.PathLike):
        self.runs_dir = Path(runs_dir)

    # -- layout ------------------------------------------------------------------
    def dir_for(self, slug: str) -> Path:
        return self.runs_dir / slug

    def db_path(self, slug: str) -> Path:
        return self.dir_for(slug) / DB_NAME

    def snapshot_path(self, slug: str) -> Path:
        return self.dir_for(slug) / SNAPSHOT_NAME

    def flowgraphs_dir(self, slug: str) -> Path:
        return self.dir_for(slug) / FLOWGRAPHS_DIR

    def learned_path(self, slug: str) -> Path:
        return self.dir_for(slug) / LEARNED_NAME

    def exists(self, name_or_slug: str) -> bool:
        try:
            return self.db_path(slugify(name_or_slug)).exists()
        except ExperimentError:
            return False

    # -- reading -----------------------------------------------------------------
    def info(self, slug: str) -> ExperimentInfo | None:
        db = self.db_path(slug)
        if not db.exists():
            return None
        store = ExperimentStore(db)
        try:
            rows = store.list_experiments(limit=1)
            if not rows:
                return ExperimentInfo(slug, slug, str(self.dir_for(slug)), "", "", None, None, 0,
                                      db.stat().st_mtime)
            e = rows[0]
            n = store.count_iterations(e.id)
            its = store.iterations(e.id, limit=1)
            last = its[-1]["created_at"] if its else e.started_at
            return ExperimentInfo(e.name, slug, str(self.dir_for(slug)), e.goal, e.backend,
                                  e.started_at, e.ended_at, n, last)
        finally:
            store.close()

    def list(self) -> list[ExperimentInfo]:
        """Every experiment under the runs directory, newest activity first."""
        if not self.runs_dir.is_dir():
            return []
        out: list[ExperimentInfo] = []
        for d in self.runs_dir.iterdir():
            if d.is_dir() and (d / DB_NAME).exists():
                i = self.info(d.name)
                if i is not None:
                    out.append(i)
        out.sort(key=lambda i: i.last_activity or 0, reverse=True)
        return out

    # -- writing -----------------------------------------------------------------
    def create(self, name: str, goal: str = "", backend: str = "") -> tuple[EditLedger, ExperimentInfo]:
        slug = slugify(name)
        if self.db_path(slug).exists():
            raise ExperimentError(f"an experiment named {name!r} already exists ({slug}); "
                                  "switch to it or choose another name")
        self.dir_for(slug).mkdir(parents=True, exist_ok=True)
        led = EditLedger(self.db_path(slug), experiment=name.strip(), goal=goal, backend=backend)
        return led, self.info(slug)  # type: ignore[return-value]

    def open(self, name_or_slug: str, backend: str = "") -> tuple[EditLedger, ExperimentInfo]:
        """Reopen an existing experiment. The directory name is the key, so a display name that
        slugs to the same thing finds it. ``backend`` is recorded if the row has none yet."""
        slug = slugify(name_or_slug)
        if not self.db_path(slug).exists():
            raise ExperimentError(f"no experiment named {name_or_slug!r} under {self.runs_dir}")
        led = EditLedger(self.db_path(slug))
        info = self.info(slug)
        if info is not None and backend and not info.backend:
            led.store.rename_experiment(led.experiment_id, info.name, None)
            # backend is provenance and only ever set once, when first measured on this host
            conn = led.store._conn()
            with led.store._write_lock:
                conn.execute("UPDATE experiment SET backend=? WHERE id=? AND backend=''",
                             (backend, led.experiment_id))
                conn.commit()
            info = self.info(slug)
        return led, info  # type: ignore[return-value]

    def rename(self, ledger: EditLedger, new_name: str, goal: str | None = None) -> ExperimentInfo:
        """Rename an OPEN experiment: the row, and the directory that carries its files.

        The caller hands back the ledger it holds because SQLite must be closed before the
        directory moves; the returned info names the new location and the caller reopens.
        """
        old_slug = ledger.path.parent.name
        new_slug = slugify(new_name)
        if new_slug != old_slug and self.db_path(new_slug).exists():
            raise ExperimentError(f"an experiment named {new_name!r} already exists ({new_slug})")
        ledger.store.rename_experiment(ledger.experiment_id, new_name.strip(), goal)
        ledger.store.close()
        if new_slug != old_slug:
            shutil.move(str(self.dir_for(old_slug)), str(self.dir_for(new_slug)))
        info = self.info(new_slug)
        assert info is not None
        return info

    def delete(self, name_or_slug: str) -> ExperimentInfo:
        """Remove an experiment and everything under its directory. IRREVERSIBLE.

        Returns what was removed -- name, slug, directory and iteration count -- so the caller can
        say exactly what it destroyed rather than reporting a bare success. The info is read
        BEFORE the files go, because afterwards there is nothing left to describe.

        The directory is resolved and checked to be a direct child of the runs directory before
        anything is unlinked. ``slugify`` already refuses a name containing a separator, so this
        cannot currently be reached -- which is the reason to keep it: a recursive delete should
        not depend for its safety on a validator two call sites away continuing to be strict.
        """
        slug = slugify(name_or_slug)
        info = self.info(slug)
        if info is None:
            raise ExperimentError(f"no experiment named {name_or_slug!r} under {self.runs_dir}")
        d = self.dir_for(slug).resolve()
        root = self.runs_dir.resolve()
        if d.parent != root or d == root:
            raise ExperimentError(f"refusing to delete {d}: not an experiment directory under {root}")
        shutil.rmtree(d)
        return info

    @staticmethod
    def now() -> float:
        return time.time()
