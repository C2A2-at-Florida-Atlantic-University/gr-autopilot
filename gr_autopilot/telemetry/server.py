"""Telemetry server (stdlib ``http.server`` only — no FastAPI/websockets).

Serves the dashboard (the built React app in ``dashboard/dist`` if present, else the stdlib
fallback page) plus a JSON snapshot at ``/data`` (the latest telemetry snapshot + edit-ledger
rows). The page polls ``/data``. Optionally accepts operator commands at ``POST /control`` (wired
to a :class:`ControlState`) — start/stop and scenario knobs for driving a demo from the browser.
That is the OPERATOR surface, deliberately distinct from the AGENT's control, which stays on the
MCP/chat surface. When no ``control`` is passed the server is read-only exactly as before. The
bench serves the pre-built static bundle; the React toolchain is a dev-time concern only.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sqlite3
import threading
import time
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gr_autopilot.tools import mcp_http

_MAX_BODY = 64 * 1024   # cap a POST body so a hostile local client can't OOM the process
log = logging.getLogger("gr_autopilot.http")

# A peer closing a keep-alive connection is the normal end of a browser tab, not a fault.
_DROPPED = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)


class _QuietServer(ThreadingHTTPServer):
    """The stdlib server prints a full traceback to stderr for every exception a handler thread
    raises -- including a browser on another machine resetting an idle connection, which it
    does constantly. Those are DEBUG; anything else is a real handler failure and is logged once,
    with its traceback, through the same logger as everything else."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, _DROPPED):
            log.debug("connection dropped by %s:%s (%s)", *client_address, type(exc).__name__)
            return
        log.exception("unhandled error serving %s:%s", *client_address)


def _cross_origin(headers) -> bool:
    """Basic CSRF guard for the operator console: reject a cross-site browser POST.

    Passes: a request with no Origin (a direct tool such as curl), a localhost Origin, and any
    Origin whose host:port equals the request's own Host -- that is a page this daemon served,
    whichever interface it is bound to (``--host 0.0.0.0`` for a portal reached over the LAN).
    Rejects: an Origin naming some other site, and a browser-declared cross-site fetch."""
    sfs = headers.get("Sec-Fetch-Site")
    if sfs and sfs not in ("same-origin", "same-site", "none"):
        return True
    origin = headers.get("Origin")
    if not origin:
        return False
    m = re.match(r"https?://([^/]+)$", origin)
    if not m:
        return True
    ohost = m.group(1)
    if re.match(r"(127\.0\.0\.1|localhost)(:\d+)?$", ohost):
        return False
    return ohost != (headers.get("Host") or "")

def _may_operate(headers, client, mcp) -> bool:
    """Who may read or drive the operator surface: POST /control, and POST /experiments.

    One credential, the bearer token on ``--token``, exactly as ``/mcp`` uses it: a client on this
    machine is inside the boundary the token draws (a local process can read it out of the daemon's
    command line anyway), and every other address must present it.

    There is deliberately no second, operator-only secret keeping the agent off this surface:
    the bench is reached over the network, the agent is therefore handed the bearer token, and one
    credential cannot separate two principals. The guarantee that matters is narrower and is
    enforced below rather than here -- the hidden channel quality is not readable from this
    surface at all (see ``_operator_view``), so an agent holding the token can still not be TOLD
    the answer it is supposed to measure.
    """
    return mcp is None or mcp._authorized(headers, client=client)


def _why_unauthorized(headers) -> str:
    """One line saying WHY the check failed, for the log and for the caller.

    'Refused' on its own cannot be acted on: a header the browser never sent and a value that does
    not match look identical from the dashboard and have opposite fixes. The token is never
    echoed, and neither is its length.
    """
    auth = (headers.get("Authorization") or "").strip()
    if not auth:
        if (headers.get("X-Operator-Password") or headers.get("X-Operator-Token") or "").strip():
            return ("the request sent an X-Operator-Password/X-Operator-Token header; the separate "
                    "operator credential was removed -- send the daemon's --token value as "
                    "'Authorization: Bearer <token>' instead")
        return ("no Authorization header was sent (on the dashboard it is the 'access token' "
                "field, which appears only when the page is opened from another machine)")
    if not auth.lower().startswith("bearer "):
        return "the Authorization header is not a Bearer credential"
    return ("the bearer token did not match the daemon's --token value")


def _operator_view(state: dict) -> dict:
    """What GET /control is allowed to say.

    ``es_n0_db`` is the operator's hidden channel setpoint -- the one number the whole experiment
    rests on the agent NOT being told, which is why the telemetry writer strips it from /data and
    why every tool response is scanned for it. With a single shared credential the agent can reach
    this endpoint, so the field is removed here too rather than guarded by who is asking. Nothing
    reads it back: the operator sets it, and the page renders tx/rx and the interferer.
    """
    return {k: v for k, v in (state or {}).items() if k != "es_n0_db"}


_CTYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
    ".svg": "image/svg+xml", ".json": "application/json", ".ico": "image/x-icon",
    ".png": "image/png", ".woff2": "font/woff2", ".map": "application/json",
}

# The control channel's whitelist: allowed operator commands + validators (clamped to safe ranges).
_CONTROL_FIELDS = {
    "running": lambda v: bool(v),
    "target_ber": lambda v: min(0.5, max(1e-6, float(v))),
    "es_n0_db": lambda v: None if v is None else min(40.0, max(-10.0, float(v))),
    "jammer": lambda v: bool(v),
    "reset": lambda v: bool(v),
    # Jammer parameters. Passed through unclamped: the instrument REFUSES an out-of-range value
    # rather than silently moving it, because a transmitter parked somewhere other than where the
    # operator believes it is invalidates the run. Absent from the default state on purpose, so a
    # daemon with no jammer reports no jammer fields.
    "jammer_freq_hz": lambda v: float(v),
    "jammer_if_gain": lambda v: int(v),
    "jammer_kind": lambda v: str(v),
    "jammer_follow": lambda v: bool(v),
    # The hidden physical condition on a hardware bench. Operator-only, like everything else here:
    # these set how hard the link is, and an agent able to move them could ease its own grade.
    # Clamped to the radios' own limits (Pluto TX attenuation 0-89 dB, RX gain -3 to 71 dB).
    "tx_atten_db": lambda v: min(89.0, max(0.0, float(v))),
    "rx_gain_db": lambda v: min(71.0, max(-3.0, float(v))),
}


class ControlState:
    """Thread-safe operator commands shared between the server (which accepts POSTs from the
    dashboard) and the driving loop (which polls). This is the dashboard's ONLY write surface —
    localhost operator controls (start/stop + scenario), distinct from the agent's own control."""

    def __init__(self, **initial):
        self._lock = threading.Lock()
        self._state = {"running": True, "target_ber": 1e-2, "es_n0_db": None, "jammer": False}
        self._state.update(self._validate(initial))

    @staticmethod
    def _validate(cmd) -> dict:
        out = {}
        for k, v in (cmd or {}).items():
            if k in _CONTROL_FIELDS:
                try:
                    out[k] = _CONTROL_FIELDS[k](v)
                except (TypeError, ValueError):
                    pass  # ignore unparseable values
        return out

    def apply(self, cmd: dict) -> dict:
        """Validate + apply an operator command; return the new public state."""
        changes = self._validate(cmd)
        with self._lock:
            self._state.update(changes)  # 'reset' rides as a one-shot flag, consumed via take_reset
        return self.get()

    def get(self) -> dict:
        with self._lock:
            return {k: v for k, v in self._state.items() if k != "reset"}

    def take_reset(self) -> bool:
        """Consume a pending one-shot reset (True once, after the operator hits Reset)."""
        with self._lock:
            return bool(self._state.pop("reset", False))


def default_webroot() -> Path:
    """Prefer the built React app; fall back to the stdlib page if it hasn't been built."""
    dist = Path(__file__).resolve().parents[2] / "dashboard" / "dist"
    return dist if (dist / "index.html").exists() else (Path(__file__).parent / "static")


def _read_ledger(path: Path, limit: int = 200) -> list[dict]:
    """Recent iterations of the most recent experiment in the database at ``path``.

    ``limit`` is applied in the query rather than by slicing afterwards, so a long-running
    experiment does not make every dashboard poll read its entire history.
    """
    from gr_autopilot.ledger.store import ExperimentStore
    from gr_autopilot.ledger.ledger import db_path_for

    if not path:
        return []
    db = db_path_for(path)
    if not Path(db).exists():
        return []
    try:
        store = ExperimentStore(db)
        experiments = store.list_experiments(limit=1)
        if not experiments:
            return []
        rows = store.iterations(experiments[0].id, limit=limit)
        store.close()
        return rows
    except sqlite3.Error:
        # A database being written concurrently, or one from a newer schema, must not take the
        # dashboard down: an empty history renders as "no iterations yet".
        return []


def _current_experiment(ledger_path: Path) -> dict | None:
    """The experiment the history is being written into, for the console header.

    The most recent row is the right one: ``EditLedger`` reuses the newest still-open experiment,
    so that is what any iteration appended now belongs to. Returns ``None`` when there is no
    database yet, which the page renders as no experiment rather than as an empty name.
    """
    from gr_autopilot.ledger.ledger import db_path_for
    from gr_autopilot.ledger.store import ExperimentStore

    if not ledger_path:
        return None
    db = db_path_for(ledger_path)
    if not Path(db).exists():
        return None
    try:
        store = ExperimentStore(db)
        rows = store.list_experiments(limit=1)
        if not rows:
            store.close()
            return None
        e = rows[0]
        out = {"id": e.id, "name": e.name, "goal": e.goal, "backend": e.backend,
               "running": e.running, "started_at": e.started_at,
               "iterations": store.count_iterations(e.id)}
        store.close()
        return out
    except sqlite3.Error:
        # Same contract as _read_ledger: a locked or newer-schema database must not take the
        # console down.
        return None


def _experiments_payload(ledger_path: Path, limit: int = 50) -> dict:
    """Every experiment in the database, with its size, best result and artifacts."""
    from gr_autopilot.ledger.ledger import db_path_for
    from gr_autopilot.ledger.store import ExperimentStore

    db = db_path_for(ledger_path)
    if not Path(db).exists():
        return {"experiments": [], "db": str(db)}
    try:
        store = ExperimentStore(db)
        out = []
        for e in store.list_experiments(limit=limit):
            best = store.best(e.id)
            out.append({
                "id": e.id, "name": e.name, "goal": e.goal, "backend": e.backend,
                "started_at": e.started_at, "ended_at": e.ended_at, "running": e.running,
                "iterations": store.count_iterations(e.id),
                "best_ber": (best or {}).get("metrics", {}).get("BER"),
                "artifacts": store.artifacts(e.id),
            })
        store.close()
        return {"experiments": out, "db": str(db)}
    except sqlite3.Error as exc:
        return {"experiments": [], "db": str(db), "error": str(exc)}


def _read_json(path: Path):
    if not path or not Path(path).exists():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _snapshot_age_s(path: Path, snap) -> float | None:
    """Seconds since the snapshot was written, or None if there is no snapshot.

    The snapshot file is a last-value store that outlives the run that produced it, so age is the
    only thing separating "the agent just measured this" from "this is where it stopped an hour
    ago". Prefer the snapshot's own ``ts`` (travels with the data, survives a file copy) and fall
    back to the file mtime for snapshots written before ``ts`` existed. Clamped at 0 so a small
    clock skew can never read as negative age (which the page would treat as fresh)."""
    if snap is None:
        return None
    ts = snap.get("ts") if isinstance(snap, dict) else None
    if not isinstance(ts, (int, float)):
        try:
            ts = Path(path).stat().st_mtime
        except OSError:
            return None
    return max(0.0, time.time() - float(ts))


def _make_handler(ledger_path, snapshot_path, webroot: Path, control: ControlState | None = None,
                  mcp=None, artifacts_dir: Path | None = None, devices=None,
                  topology=None, experiments=None):
    wr = webroot.resolve()

    # The current experiment can change while the server runs, so every path that depends on it
    # is resolved per request. A plain value (the old contract, used by the scripts) still works.
    def _resolve(v):
        v = v() if callable(v) else v
        return Path(v) if v else None

    def _ledger():
        return _resolve(ledger_path)

    def _snapshot():
        return _resolve(snapshot_path)

    def _artifacts():
        v = _resolve(artifacts_dir)
        return v.resolve() if v else None

    class Handler(BaseHTTPRequestHandler):
        # Keep-alive matters here: an MCP client issues many small POSTs, and HTTP/1.0 would
        # force a new TCP connection per tool call.
        protocol_version = "HTTP/1.1"
        # An idle keep-alive connection is reaped rather than held forever; the timeout surfaces
        # as a quiet close, not a traceback.
        timeout = 120

        def log_message(self, fmt, *args):
            # The stdlib's access line, demoted to DEBUG: with the dashboard polling twice a
            # second it is a firehose, but it is exactly what you want when a request misbehaves.
            log.debug("%s:%s %s", self.client_address[0], self.client_address[1], fmt % args)

        def log_error(self, fmt, *args):
            log.warning("%s:%s %s", self.client_address[0], self.client_address[1], fmt % args)

        def _send(self, code: int, ctype: str, body: bytes, close: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self, code: int, body: bytes, ctype: str = "application/json"):
            """Answer a POST that is rejected BEFORE its body has been read.

            HTTP/1.1 keeps the connection alive, so a body left unread in the socket is parsed as
            the next request LINE: one refused POST turns every following request on that
            connection into 'Bad request syntax (...)' -- the caller sees a rejected write and
            then a burst of unrelated 400s on requests it never malformed. Drain the body first,
            or close the connection when it is too large (or chunked) to drain cheaply.
            """
            n = int(self.headers.get("Content-Length", 0) or 0)
            if 0 < n <= _MAX_BODY:
                try:
                    self.rfile.read(n)
                except OSError:
                    self.close_connection = True
            elif n > _MAX_BODY or self.headers.get("Transfer-Encoding"):
                return self._send(code, ctype, body, close=True)
            return self._send(code, ctype, body)

        def _send_raw(self, triple):
            """Send an (status, headers, body) triple produced by a transport adapter."""
            status, headers, body = triple
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        # -- MCP (Model Context Protocol) endpoint ---------------------------
        # Registered BEFORE the static catch-all in do_GET: every unknown path falls through to
        # the dashboard's index.html, so a route added after it would silently serve the web app
        # instead of erroring, and a test asserting only on the status code would still pass.
        def do_DELETE(self):
            if mcp is not None and self.path.split("?", 1)[0] == "/mcp":
                return self._send_raw(mcp.delete(self.headers, client=self.client_address[0]))
            return self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if mcp is not None and self.path.split("?", 1)[0] == "/mcp":
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n > mcp_http.MAX_BODY:
                    return self._refuse(413, b'{"error":"body too large"}')
                return self._send_raw(mcp.post(self.rfile.read(n) if n else b"", self.headers,
                                               client=self.client_address[0]))
            # Experiment lifecycle from the dashboard: start, switch, rename, delete. Same trust rule as
            # /mcp (which can do the same things): loopback is exempt, anywhere else needs the
            # bearer token. Origin is checked so a page on another site cannot drive it.
            if self.path.split("?", 1)[0] == "/experiments" and experiments is not None:
                if _cross_origin(self.headers):
                    log.warning("POST /experiments from %s refused: cross-origin", self.client_address[0])
                    return self._refuse(403, b'{"error":"cross-origin blocked"}')
                if not _may_operate(self.headers, self.client_address[0], mcp):
                    why = _why_unauthorized(self.headers)
                    log.warning("POST /experiments from %s refused off-loopback: %s",
                                self.client_address[0], why)
                    return self._refuse(401, json.dumps(
                        {"error": "off the bench host, naming or switching an experiment needs the "
                                  "access token -- " + why,
                         "code": "token_required"}).encode("utf-8"))
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n > _MAX_BODY:
                    return self._refuse(413, b'{"error":"body too large"}')
                try:
                    cmd = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except (json.JSONDecodeError, ValueError):
                    return self._send(400, "application/json", b'{"error":"bad json"}')
                action = str(cmd.get("action", ""))
                name, goal = str(cmd.get("name", "")), cmd.get("goal")
                try:
                    if action == "start":
                        out = experiments.start_experiment(name, goal or "")
                    elif action == "switch":
                        out = experiments.switch_experiment(name)
                    elif action == "rename":
                        out = experiments.rename_experiment(name, goal)
                    elif action == "delete":
                        # Operator-only and irreversible. It is on THIS surface and not on /mcp
                        # because an agent that can delete a previous run's measurements can
                        # destroy evidence it was meant to produce.
                        out = experiments.delete_experiment(name)
                    else:
                        return self._send(400, "application/json",
                                          b'{"error":"action must be start, switch, rename or delete"}')
                except Exception as exc:  # noqa: BLE001 - a refused lifecycle command is a 400
                    code = getattr(exc, "code", "error")
                    msg = getattr(exc, "message", str(exc))
                    log.warning("experiment %s %r from %s refused: %s", action, name,
                                self.client_address[0], msg)
                    return self._send(400, "application/json",
                                      json.dumps({"error": msg, "code": code}).encode("utf-8"))
                log.info("experiment %s %r from %s -> %s", action, name, self.client_address[0],
                         out.get("action"))
                return self._send(200, "application/json", json.dumps(out).encode("utf-8"))
            # The one other write surface: operator commands from the dashboard. No-op if read-only.
            if self.path.split("?", 1)[0] == "/control" and control is not None:
                if _cross_origin(self.headers):     # CSRF: a page on another origin can't drive it
                    return self._refuse(403, b'{"error":"cross-origin blocked"}')
                # The operator/agent split, enforced by a secret rather than by URL convention.
                # The agent reaches /mcp on THIS port, so without this an agent able to POST could
                # switch off the very interference it is being graded on adapting to.
                if not _may_operate(self.headers, self.client_address[0], mcp):
                    why = _why_unauthorized(self.headers)
                    log.warning("POST /control from %s refused: %s", self.client_address[0], why)
                    return self._refuse(403, json.dumps(
                        {"error": "access token required -- " + why,
                         "code": "token_required"}).encode("utf-8"))
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n > _MAX_BODY:
                    return self._refuse(413, b'{"error":"body too large"}')
                try:
                    cmd = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except (json.JSONDecodeError, ValueError):
                    return self._send(400, "application/json", b'{"error":"bad json"}')
                if not isinstance(cmd, dict):
                    return self._send(400, "application/json", b'{"error":"expected an object"}')
                try:
                    state = control.apply(cmd)
                except Exception as exc:  # noqa: BLE001 - a refused operator command is a 400
                    log.warning("operator command %s from %s refused: %s", cmd,
                                self.client_address[0], exc)
                    return self._send(400, "application/json",
                                      json.dumps({"error": str(exc)}).encode("utf-8"))
                if cmd:   # an empty POST is a read; anything else changed the scenario
                    log.info("operator command from %s: %s", self.client_address[0], cmd)
                return self._send(200, "application/json", json.dumps(state).encode("utf-8"))
            return self._refuse(404, b"not found", "text/plain")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if mcp is not None and path == "/mcp":
                return self._send_raw(mcp.get(self.headers))   # 405: no SSE stream offered
            # Reading back the operator's own settings, behind the same password that writes
            # them. Without this the dashboard's "Check" button had nothing to check against:
            # an unhandled GET fell through to the static catch-all, the page got HTML where it
            # expected JSON, and a correctly entered password still displayed as unverified.
            # The state includes the hidden channel setpoint, which is exactly why it is gated --
            # the agent reaches /mcp on this same port and must never read it.
            if path == "/control" and control is not None:
                if not _may_operate(self.headers, self.client_address[0], mcp):
                    why = _why_unauthorized(self.headers)
                    return self._refuse(403, json.dumps(
                        {"error": "access token required -- " + why,
                         "code": "token_required"}).encode("utf-8"))
                return self._send(200, "application/json",
                                  json.dumps(_operator_view(control.get())).encode("utf-8"))
            # Documentation. Registered before the static catch-all below, which answers every
            # unknown path with the dashboard's page -- a route added after it would appear to
            # work while serving the web application instead.
            if path == "/wiki" or path.startswith("/wiki/"):
                from urllib.parse import parse_qs
                from gr_autopilot import wiki
                name = path[len("/wiki/"):].strip("/") or "index"
                query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                # The portal asks for the page as data and renders it inside its own shell.
                if "fragment" in query:
                    frag = wiki.render_fragment(name)
                    if frag is None:
                        body = json.dumps({"error": "no such page", "pages": wiki.page_index()})
                        return self._send(404, "application/json", body.encode("utf-8"))
                    return self._send(200, "application/json", json.dumps(frag).encode("utf-8"))
                # A browser navigating here is sent into the portal so the documentation shares
                # the dashboard's layout; anything else (curl, tests, a client without script)
                # gets the standalone rendering below.
                if "text/html" in (self.headers.get("Accept") or ""):
                    return self._send_raw((302, {"Location": f"/#/docs/{name}",
                                                 "Content-Length": "0"}, b""))
                page = wiki.render_page(name)
                if page is None:
                    body = ("<h1>No such page</h1><p>Known pages: " + ", ".join(
                        f'<a href="{p["url"]}">{p["title"]}</a>' for p in wiki.page_index())
                        + "</p>")
                    return self._send(404, "text/html; charset=utf-8", body.encode("utf-8"))
                return self._send(200, "text/html; charset=utf-8", page.html.encode("utf-8"))
            # The experiment library. Served here as well as over the tool protocol so the
            # portal can show it to a reader who has not connected a client yet.
            if path.rstrip("/") == "/prompts":
                from gr_autopilot.tools import prompts as _prompts
                return self._send(200, "application/json",
                                  json.dumps({"prompts": _prompts.catalog()}).encode("utf-8"))
            # Per-device liveness. ``devices`` is a callable so the answer is measured when
            # asked rather than being whatever was true when the server started -- the entire
            # point is that a radio can disappear between one poll and the next.
            if path.rstrip("/") == "/devices":
                if devices is None:
                    payload = {"devices": {}, "backend": "simulated",
                               "note": "no radios are attached to this server"}
                else:
                    try:
                        payload = devices()
                    except Exception as exc:  # noqa: BLE001 - never take the page down
                        payload = {"devices": {}, "error": str(exc)}
                return self._send(200, "application/json", json.dumps(payload).encode("utf-8"))
            # The declared bench and whether the hardware agrees with it. Operator-side: it
            # carries the attestation and the verification verdict, neither of which belongs on
            # the agent's tool surface.
            if path.rstrip("/") == "/topology":
                if topology is None:
                    payload = {"declared": False,
                               "note": "no topology declared — device identity is unverifiable"}
                else:
                    try:
                        payload = {"declared": True, **topology()}
                    except Exception as exc:  # noqa: BLE001 - never take the page down
                        payload = {"declared": True, "error": str(exc)}
                return self._send(200, "application/json", json.dumps(payload).encode("utf-8"))
            # The experiment record: what has been run, and what each run produced. Grouped by
            # experiment, which the previous flat log could not express.
            if path.rstrip("/") == "/experiments" and (experiments is not None or _ledger()):
                if experiments is not None:
                    cur = experiments.current_experiment()
                    payload = {"experiments": experiments.list_saved_experiments(),
                               "current": cur.get("slug") if cur.get("selected") else None,
                               "runs_dir": str(experiments.manager.runs_dir)}
                else:
                    payload = _experiments_payload(_ledger())
                return self._send(200, "application/json", json.dumps(payload).encode())
            # Exported GNU Radio Companion files, so the flowgraph an experiment produced is
            # reachable from the portal rather than only present on disk. Listed at
            # /flowgraphs, downloaded at /flowgraphs/<name>.grc.
            ar = _artifacts()
            if ar is not None and path.rstrip("/") == "/flowgraphs":
                files = sorted(p.name for p in ar.glob("*.grc")) if ar.is_dir() else []
                body = json.dumps({"flowgraphs": files, "dir": str(ar),
                                   "open_with": "gnuradio-companion"}).encode("utf-8")
                return self._send(200, "application/json", body)
            if ar is not None and path.startswith("/flowgraphs/"):
                target = (ar / path[len("/flowgraphs/"):]).resolve()
                if ar not in target.parents or target.suffix != ".grc":
                    return self._send(403, "text/plain", b"forbidden")
                if not target.is_file():
                    return self._send(404, "text/plain", b"no such flowgraph")
                return self._send(200, "application/x-yaml", target.read_bytes())
            if path == "/data":
                # cap the ledger tail so /data stays lean on a long-running demo (the dashboard
                # only renders the most recent rows anyway)
                lp, sp = _ledger(), _snapshot()
                rows = _read_ledger(lp, limit=200) if lp else []
                snap = _read_json(sp) if sp else None
                if experiments is not None:
                    cur = experiments.current_experiment()
                    exp = cur if cur.get("selected") else None
                else:
                    exp = _current_experiment(lp) if lp else None
                # age_s lets the page distinguish a live run from a stopped one. Computed here
                # rather than client-side so it never depends on the browser clock matching ours.
                payload = {"ledger": rows, "snapshot": snap,
                           "age_s": _snapshot_age_s(sp, snap),
                           # Which experiment these rows belong to (None = nothing selected: the
                           # page asks for a name), and whether it came from radios or a model.
                           "experiment": exp}
                return self._send(200, "application/json", json.dumps(payload).encode("utf-8"))
            rel = path.lstrip("/") or "index.html"
            target = (wr / rel).resolve()
            if not (target == wr or wr in target.parents):  # no path traversal
                return self._send(403, "text/plain", b"forbidden")
            if not target.is_file():
                target = wr / "index.html"  # serve the app shell for any unknown path
            self._send(200, _CTYPES.get(target.suffix, "application/octet-stream"),
                       target.read_bytes())

    return Handler


def serve(ledger_path=None, snapshot_path=None, port: int = 8080, host: str = "127.0.0.1",
          webroot: Path | None = None, background: bool = False, control: ControlState | None = None,
          mcp=None, artifacts_dir: Path | None = None, devices=None,
          topology=None, experiments=None):
    """Start the telemetry server. ``background=True`` returns the server after starting it in a
    daemon thread (for single-process demos that run the loop and serve at once). Pass ``control``
    to accept operator commands at ``POST /control``; omit it to stay read-only. Pass ``mcp``
    (an :class:`~gr_autopilot.tools.mcp_http.MCPHttpEndpoint`) to also speak the Model Context
    Protocol at ``/mcp``, so one process serves the dashboard and the agent's tool surface.

    ``POST /control`` and ``POST /experiments`` are guarded by the same bearer token as ``/mcp``,
    passed on ``mcp``; without an ``mcp`` endpoint they are unguarded, which is only acceptable on
    loopback."""
    root = Path(webroot) if webroot else default_webroot()
    httpd = _QuietServer(
        (host, port),
        _make_handler(ledger_path, snapshot_path, root, control, mcp, artifacts_dir, devices,
                      topology, experiments))
    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd
    httpd.serve_forever()
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description="gr-autopilot telemetry dashboard server")
    ap.add_argument("--ledger", default="", help="edit-ledger path (a .jsonl name maps to a sibling .db)")
    ap.add_argument("--snapshot", default="", help="path to the telemetry snapshot JSON")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    root = default_webroot()
    print(f"telemetry dashboard: http://127.0.0.1:{args.port}  (serving {root.name}/, "
          f"ledger={args.ledger or '-'}, snapshot={args.snapshot or '-'})")
    serve(args.ledger or None, args.snapshot or None, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
