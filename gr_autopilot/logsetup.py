"""One logging configuration for the daemon and everything it hosts.

Until now the daemon printed a startup banner and otherwise let the standard library speak for
it, which meant the console was quiet about the things that matter (a tool call that failed, an
operator arming the interferer, a worker dying, an experiment switching) and loud about the one
thing that does not (a browser on another machine dropping a keep-alive connection, which the
stdlib HTTP server reports as a full traceback, every time).

Two sinks, one format family:

* the console, at ``--log-level`` (default INFO): one line per event, time / level / component /
  message, no tracebacks below ERROR;
* a rotating file beside the runs directory (default ``<runs-dir>/autopilotd.log``, 5 MB x 5),
  always at DEBUG, with tracebacks -- the record to read when something went wrong an hour ago.

Component names are the logger names: ``gr_autopilot.mcp`` (tool calls), ``gr_autopilot.http``
(the portal and the operator surface), ``gr_autopilot.service`` (experiments, resets),
``gr_autopilot.worker`` (the radio subprocess), ``gr_autopilot.jammer`` (the interferer).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-22s %(threadName)s  %(message)s"
DATE_FORMAT = "%H:%M:%S"
FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED_KEY = "_gr_autopilot_configured"


class _ShortName(logging.Filter):
    """``gr_autopilot.tools.mcp_stdio`` -> ``mcp``: the console column stays narrow."""
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("gr_autopilot."):
            record.name = record.name[len("gr_autopilot."):]
        return True


def configure(level: str = "info", log_file: str | os.PathLike | None = None,
              stream=None) -> Path | None:
    """Install the handlers. Idempotent: calling it again replaces the previous ones, so tests
    and a re-entrant ``main`` do not accumulate duplicates. Returns the log file path, if any."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _CONFIGURED_KEY, False):
            root.removeHandler(h)
            h.close()

    lvl = getattr(logging, str(level).upper(), None)
    if not isinstance(lvl, int):
        raise ValueError(f"unknown log level {level!r} (use debug, info, warning or error)")

    console = logging.StreamHandler(stream or sys.stderr)
    console.setLevel(lvl)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, DATE_FORMAT))
    console.addFilter(_ShortName())
    setattr(console, _CONFIGURED_KEY, True)
    root.addHandler(console)

    path = None
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5,
                                                  encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(FILE_FORMAT, FILE_DATE_FORMAT))
        fh.addFilter(_ShortName())
        setattr(fh, _CONFIGURED_KEY, True)
        root.addHandler(fh)

    # The root must let DEBUG through for the file; the console filters at its own level.
    root.setLevel(min(lvl, logging.DEBUG if path else lvl))
    # Third-party chatter that never helps diagnose a bench.
    for noisy in ("matplotlib", "PIL", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def summarize_args(args: dict, limit: int = 160) -> str:
    """A one-line view of tool arguments for the INFO log: the identifying fields, not the
    payload. Full arguments go to the DEBUG line."""
    keep = {k: v for k, v in (args or {}).items()
            if k in ("name", "n_bits", "device_id", "role", "center_freq_hz", "freqs_hz",
                     "channels_hz", "hop_rate_hz", "power_db", "iteration", "path", "compact",
                     "max_points", "target_ber", "budget")}
    spec = (args or {}).get("spec")
    if isinstance(spec, dict):
        keep["spec"] = f"{spec.get('structure_id')}/{spec.get('modulation')}"
    s = ", ".join(f"{k}={v}" for k, v in keep.items())
    return s if len(s) <= limit else s[: limit - 1] + "…"
