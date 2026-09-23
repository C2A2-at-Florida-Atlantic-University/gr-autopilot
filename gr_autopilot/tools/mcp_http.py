"""MCP over Streamable HTTP -- the transport half only, no protocol logic.

The Model Context Protocol (MCP) defines two standard transports: stdio, where the client
launches the server as a child process and speaks over its pipes, and Streamable HTTP, where the
server listens on a URL and the client POSTs JSON-RPC to it. ``gr_autopilot.tools.mcp_stdio``
implements the first. This module implements the second over the SAME
:class:`~gr_autopilot.tools.mcp_stdio.StdioMCPServer`, because that class already separates
protocol from transport: ``handle(msg)`` takes a decoded JSON-RPC message and returns a response
(or ``None`` for a notification), and knows nothing about pipes.

Why this exists at all. Under stdio the CLIENT spawns the server, so the server's lifetime is the
client's, there is exactly one client, and there is no second channel for anyone else to reach it.
That is wrong for a laboratory instrument: the radios must be owned by a process that outlives any
one chat session, the dashboard has to read from that same process, and the human operator needs a
control surface the agent cannot see. A listening socket gives all three.

Deliberately NOT implemented in this version:
  * Server-Sent Events (SSE). The specification lets a server answer a POST with either a single
    JSON response or an SSE stream, and lets clients open a long-lived GET stream for
    server-initiated messages. Only the single-JSON form is implemented; GET returns 405, which
    the specification explicitly permits. Long tool calls therefore block until they finish. This
    will need revisiting for progress reporting (``start_bo_run`` can be a dozen physical trials
    inside one request).
  * Resumability (``Last-Event-ID``) and server-initiated requests, both of which require SSE.

Security. The endpoint binds to the loopback interface and validates the ``Origin`` header,
because a browser page on another site can otherwise POST to a localhost server (a DNS-rebinding
attack). An optional bearer token gates access. Request bodies are capped.
"""
from __future__ import annotations

import logging

import json
import re
import secrets
from typing import Any

from gr_autopilot.tools.mcp_stdio import StdioMCPServer, _dumps

MAX_BODY = 1 << 20          # 1 MiB: a flowgraph spec is small; anything larger is a mistake
PROTOCOL_HEADER = "MCP-Protocol-Version"
SESSION_HEADER = "Mcp-Session-Id"

#: Peer addresses that are exempt from the bearer token: the machine boundary is their auth.
_log = logging.getLogger("gr_autopilot.mcp")

LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

_LOCAL_ORIGIN = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")


class MCPHttpEndpoint:
    """Streamable-HTTP front end for one :class:`StdioMCPServer`.

    Framework-agnostic on purpose: :meth:`post` takes the pieces of an HTTP request and returns
    ``(status, headers, body)``, so the caller can be ``http.server``, or anything else later,
    without this module importing a web framework.
    """

    def __init__(self, server: StdioMCPServer, *, token: str | None = None,
                 require_session: bool = True):
        self.server = server
        self.token = token or None
        self.require_session = bool(require_session)
        self.sessions: set[str] = set()

    # -- guards --------------------------------------------------------------
    def _authorized(self, headers, client: str | None = None) -> bool:
        """The bearer token guards the NETWORK. A client on this machine is already inside the
        boundary the token draws -- a local process could read the token from the command line
        anyway -- so loopback is exempt and a ``--token`` daemon keeps working for the local client
        while every other address must present it."""
        if self.token is None or client in LOOPBACK_CLIENTS:
            return True
        auth = (headers.get("Authorization") or "").strip()
        if not auth.lower().startswith("bearer "):
            return False
        # constant-time compare so a token cannot be recovered by timing the response
        return secrets.compare_digest(auth[7:].strip(), self.token)

    @staticmethod
    def _origin_ok(headers) -> bool:
        """Reject cross-site browser requests (DNS-rebinding guard).

        A non-browser client (curl, an MCP client) sends no Origin at all, which is allowed; a
        browser always sends one, and only a loopback origin is accepted.
        """
        origin = headers.get("Origin")
        if not origin:
            return True
        return bool(_LOCAL_ORIGIN.match(origin.strip()))

    # -- responses -----------------------------------------------------------
    @staticmethod
    def _json(status: int, payload: Any, extra: dict | None = None):
        body = _dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body)),
                   "Cache-Control": "no-store"}
        headers.update(extra or {})
        return status, headers, body

    @classmethod
    def _rpc_error(cls, status: int, mid, code: int, message: str):
        return cls._json(status, {"jsonrpc": "2.0", "id": mid,
                                  "error": {"code": code, "message": message}})

    @staticmethod
    def _is_initialize(msg) -> bool:
        msgs = msg if isinstance(msg, list) else [msg]
        return any(isinstance(m, dict) and m.get("method") == "initialize" for m in msgs)

    # -- the transport -------------------------------------------------------
    def post(self, body: bytes, headers, client: str | None = None) -> tuple[int, dict, bytes]:
        """Handle one POST. ``headers`` is any mapping with a case-insensitive ``get``;
        ``client`` is the peer address, when the caller knows it."""
        if not self._origin_ok(headers):
            _log.warning("POST /mcp from %s refused: cross-origin", client)
            return self._json(403, {"error": "cross-origin request blocked"})
        if not self._authorized(headers, client):
            _log.warning("POST /mcp from %s refused: unauthorized (bearer token required off-loopback)", client)
            return self._json(401, {"error": "unauthorized"},
                              {"WWW-Authenticate": "Bearer"})
        if len(body) > MAX_BODY:
            return self._rpc_error(413, None, -32600, "request body too large")

        try:
            msg = json.loads(body or b"")
        except (json.JSONDecodeError, ValueError):
            return self._rpc_error(400, None, -32700, "parse error")

        # Session handling. The server issues an id on initialize; later requests must present it.
        # An unknown id gets 404, which tells a well-behaved client to start a new session rather
        # than retry forever (this is how the specification says to signal an expired session).
        is_init = self._is_initialize(msg)
        session_id = headers.get(SESSION_HEADER)
        if self.require_session and not is_init:
            if not session_id:
                return self._rpc_error(400, None, -32600,
                                       f"missing {SESSION_HEADER}; call initialize first")
            if session_id not in self.sessions:
                _log.warning("POST /mcp from %s with unknown session %s…", client, str(session_id)[:8])
                return self._rpc_error(404, None, -32600, "unknown or expired session")

        try:
            resp = self.server.handle(msg)
        except Exception as exc:  # one bad message must never take the daemon down
            _log.exception("MCP message from %s raised", client)
            mid = msg.get("id") if isinstance(msg, dict) else None
            return self._rpc_error(200, mid, -32603, f"internal error: {exc}")

        # A payload of only notifications/responses produces nothing to send. The specification
        # says to answer 202 Accepted with an empty body -- NOT an empty JSON-RPC envelope.
        if resp is None:
            return 202, {"Content-Length": "0", "Cache-Control": "no-store"}, b""

        extra = {}
        if is_init:
            session_id = secrets.token_urlsafe(24)
            self.sessions.add(session_id)
            extra[SESSION_HEADER] = session_id
            info = (msg.get("params") or {}).get("clientInfo", {}) if isinstance(msg, dict) else {}
            _log.info("MCP session opened by %s (%s %s), %d sessions live", client,
                      info.get("name", "?"), info.get("version", ""), len(self.sessions))
        return self._json(200, resp, extra)

    def delete(self, headers, client: str | None = None) -> tuple[int, dict, bytes]:
        """Explicit session termination (the specification's DELETE on the endpoint)."""
        if not self._origin_ok(headers):
            return self._json(403, {"error": "cross-origin request blocked"})
        if not self._authorized(headers, client):
            return self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
        sid = headers.get(SESSION_HEADER)
        self.sessions.discard(sid)
        _log.info("MCP session closed by %s, %d sessions live", client, len(self.sessions))
        return 204, {"Content-Length": "0"}, b""

    def get(self, headers) -> tuple[int, dict, bytes]:
        """GET opens the server-to-client SSE stream. Not supported here; the specification
        allows answering 405, and clients must treat that as 'this server has no stream'."""
        return self._json(405, {"error": "this server does not offer an SSE stream; POST instead"},
                          {"Allow": "POST, DELETE"})
