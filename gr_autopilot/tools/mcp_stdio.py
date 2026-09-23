"""A dependency-free MCP server over stdio.

MCP's stdio transport is just newline-delimited JSON-RPC 2.0 on stdin/stdout, so the whole
protocol is a small state machine -- no `mcp` SDK required (installing it would pull a pip tree
that breaks the bench's system numpy/scipy; see project memory). This lets any MCP client
(Claude Desktop, Claude Code, ...) drive the real gr-autopilot loop:

    claude mcp add gr-autopilot -- python -m gr_autopilot.mcp_server
    # or in an MCP client config:  { "command": "python", "args": ["-m", "gr_autopilot.mcp_server"] }

The tools come from ``gr_autopilot.tools.manifest`` (bound to ``AutopilotService``); this module
only speaks the wire protocol. ``InProcessClient`` drives the same server object without a pipe,
for tests and the reference driver.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger("gr_autopilot.mcp")

import json
import math
import sys
from pathlib import Path

from gr_autopilot.tools import prompts
from gr_autopilot.tools.manifest import INSTRUCTIONS, Tool, tool_manifest
from gr_autopilot.tools.service import AutopilotService, ToolError

# MCP revisions we speak; we echo the client's requested version when it is one of these.
_DEFAULT_PROTOCOL = "2024-11-05"
_SUPPORTED = {"2024-11-05", "2025-03-26", "2025-06-18"}
SERVER_INFO = {"name": "gr-autopilot", "version": "0.1.0"}


def _json_default(o):
    """Make numpy scalars/arrays JSON-serializable (service dicts are mostly plain already)."""
    import numpy as np
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")


def _sanitize(o):
    """Recursively coerce non-finite floats (inf/NaN — e.g. SNR on a zero-error link, or a diverged
    DSP block) to None so the result is VALID JSON. Bare Infinity/NaN tokens are rejected by strict
    clients (JS JSON.parse in Claude Desktop) and corrupt the appended JSONL transcript."""
    import numpy as np
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, np.floating):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return [_sanitize(v) for v in o.tolist()]
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(v) for v in o]
    return o


def _dumps(obj) -> str:
    """JSON with non-finite values coerced to null and allow_nan=False, so nothing invalid reaches
    the wire or the transcript."""
    return json.dumps(_sanitize(obj), allow_nan=False)


def estimate_tokens(obj) -> int:
    """Rough token proxy (~4 chars/token) for JSON crossing the tool surface. The server cannot see
    the client model's real tokenizer, so this measures the *tool-surface footprint* the model must
    emit (arguments) and read back (results) -- a fair, transport-level proxy for token cost."""
    try:
        return max(1, len(json.dumps(obj, default=_json_default)) // 4)
    except (TypeError, ValueError):
        return 1


class StdioMCPServer:
    """JSON-RPC 2.0 dispatcher for the MCP tool surface. Transport-free: feed it decoded request
    dicts via ``handle()`` (returns a response dict or None for notifications), or run ``serve()``
    to read/write newline-delimited JSON on real streams."""

    def __init__(self, service: AutopilotService | None = None, tools: list[Tool] | None = None,
                 log_path: str | Path | None = None, instructions: str = INSTRUCTIONS):
        self.service = service or AutopilotService()
        self.tools = tools or tool_manifest()
        self._by_name = {t.name: t for t in self.tools}
        self.instructions = instructions
        self.log_path = Path(log_path) if log_path else None
        self.initialized = False
        # Token accounting: the tool-surface footprint the driving LLM must read/emit. Accumulated
        # overall, per tool, and per "attempt" (a run_flowgraph -- one physical link trial -- closes
        # the current attempt). A proxy, not the client's true tokenizer (see estimate_tokens).
        self.tok_total = 0
        self.tok_calls = 0
        self.tok_by_tool: dict[str, int] = {}
        self.tok_attempts: list[dict] = []   # [{tokens, calls}], one per completed attempt
        self._attempt_tokens = 0
        self._attempt_calls = 0
        # Optional hook fired after each successful run_flowgraph (one physical link trial). A demo
        # driver uses it to write a telemetry snapshot -- keeps telemetry out of the tool surface.
        self.on_run_flowgraph = None

    # -- logging (the transcript artifact) -----------------------------------
    def _log(self, direction: str, payload) -> None:
        if self.log_path is None:
            return
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(_dumps({"dir": direction, "msg": payload}) + "\n")

    # -- JSON-RPC helpers ----------------------------------------------------
    @staticmethod
    def _ok(mid, result) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    # -- dispatch ------------------------------------------------------------
    def handle(self, msg):
        """Handle one decoded JSON-RPC message (or a batch list). Returns a response dict, a list
        of responses for a batch, or None when nothing is to be sent (notifications). Logs each
        request/response here so the transcript is captured regardless of transport (serve() over
        real pipes or InProcessClient in tests/drivers)."""
        if isinstance(msg, list):
            if not msg:                       # JSON-RPC: an empty batch is a single Invalid Request
                return self._err(None, -32600, "invalid request: empty batch")
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        self._log("recv", msg)
        resp = self._dispatch(msg)
        if resp is not None:
            self._log("send", resp)
        return resp

    def _dispatch(self, msg: dict):
        if not isinstance(msg, dict):
            # A valid JSON value that isn't an object (e.g. `42`, `"x"`, `[1]` element) is an Invalid
            # Request, NOT a reason to crash the read loop with an AttributeError on msg.get(...).
            return self._err(None, -32600, "invalid request: expected a JSON-RPC object")
        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion")
            version = requested if requested in _SUPPORTED else _DEFAULT_PROTOCOL
            return self._ok(mid, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False},
                                 "prompts": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": self.instructions,
            })
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": [t.to_mcp() for t in self.tools]})
        if method == "tools/call":
            return self._call(mid, msg.get("params") or {})
        # Be a good citizen for optional surfaces some clients probe on connect.
        if method == "resources/list":
            return self._ok(mid, {"resources": []})
        if method == "resources/templates/list":
            return self._ok(mid, {"resourceTemplates": []})
        # The experiment library. These are prompts and nothing else -- no saved flowgraph, no
        # fixture -- so a client can offer a reader a starting point without the system having
        # prepared the answer.
        if method == "prompts/list":
            return self._ok(mid, {"prompts": prompts.list_prompts()})
        if method == "prompts/get":
            params = msg.get("params") or {}
            try:
                return self._ok(mid, prompts.get_prompt(params.get("name"),
                                                        params.get("arguments") or {}))
            except KeyError:
                return self._err(mid, -32602, f"unknown prompt: {params.get('name')!r}")

        if mid is None:
            return None  # unknown notification -> ignore silently
        return self._err(mid, -32601, f"method not found: {method}")

    def _call(self, mid, params: dict):
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = self._by_name.get(name)
        if tool is None:
            return self._err(mid, -32602, f"unknown tool: {name}")
        # Validate required args against the tool's inputSchema up front, so a client mistake reads as
        # -32602 invalid_params (actionable) rather than a KeyError surfaced as an 'internal' error.
        missing = [k for k in tool.input_schema.get("required", []) if k not in args]
        if missing:
            return self._err(mid, -32602, f"invalid params for {name}: missing required {missing}")
        from gr_autopilot.logsetup import summarize_args
        t0 = time.monotonic()
        try:
            data, is_error = tool.handler(self.service, args), False
        except ToolError as exc:
            # A tool-level error is a normal MCP result with isError=true (the model can react to
            # it), NOT a JSON-RPC protocol error.
            data, is_error = {"error": {"code": exc.code, "message": exc.message}}, True
        except Exception as exc:  # defensive; surface as a tool error -- and LOG it, with the
            # traceback: a swallowed internal error is the one kind nobody can diagnose later.
            _log.exception("tool %s raised", name)
            data, is_error = {"error": {"code": "internal", "message": str(exc)}}, True
        ms = (time.monotonic() - t0) * 1000.0
        if is_error:
            _log.warning("tool %s -> error %s (%.0f ms) %s", name, data["error"]["code"], ms,
                         summarize_args(args))
        else:
            _log.info("tool %s ok (%.0f ms) %s", name, ms, summarize_args(args))
        _log.debug("tool %s args=%s", name, args)
        self._account_tokens(name, params, data)
        if name == "run_flowgraph" and not is_error and self.on_run_flowgraph is not None:
            try:
                self.on_run_flowgraph(self)
            except Exception:  # a telemetry hook must never break the tool call -- but say so
                _log.exception("telemetry hook failed after %s", name)
        return self._tool_result(mid, data, is_error=is_error)

    def _account_tokens(self, name: str, params: dict, data) -> None:
        est = estimate_tokens(params) + estimate_tokens(data)  # emitted args + read-back result
        self.tok_total += est
        self.tok_calls += 1
        self.tok_by_tool[name] = self.tok_by_tool.get(name, 0) + est
        self._attempt_tokens += est
        self._attempt_calls += 1
        if name == "run_flowgraph":  # a physical link trial closes an attempt
            self.tok_attempts.append({"tokens": self._attempt_tokens, "calls": self._attempt_calls})
            self._attempt_tokens = 0
            self._attempt_calls = 0

    def token_stats(self) -> dict:
        """Snapshot of the tool-surface token footprint: overall, per attempt, and per tool."""
        n = len(self.tok_attempts)
        return {
            "total": self.tok_total, "calls": self.tok_calls, "attempts": n,
            "this_attempt": self._attempt_tokens,
            "avg_per_attempt": round(self.tok_total / n) if n else 0,
            "per_attempt": [a["tokens"] for a in self.tok_attempts],
            "by_tool": dict(self.tok_by_tool), "estimated": True,
        }

    def _tool_result(self, mid, data, is_error: bool):
        text = _dumps(data)
        result = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        return self._ok(mid, result)

    # -- transport -----------------------------------------------------------
    def serve(self, stdin=None, stdout=None) -> None:
        """Read newline-delimited JSON-RPC from ``stdin`` and write responses to ``stdout``."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                resp = self._err(None, -32700, "parse error")  # handle() logs; this path can't
                self._log("recv", line)
            else:
                try:
                    resp = self.handle(msg)  # logs recv+send itself
                except Exception as exc:  # a single bad message must never kill the serve loop
                    mid = msg.get("id") if isinstance(msg, dict) else None
                    resp = self._err(mid, -32603, f"internal error: {exc}")
            if resp is None:
                continue
            stdout.write(_dumps(resp) + "\n")
            stdout.flush()


class MCPToolError(RuntimeError):
    """A tool returned isError=true (raised by InProcessClient.call for ergonomic driving)."""

    def __init__(self, error: dict):
        self.code = error.get("code", "error")
        self.message = error.get("message", "")
        super().__init__(f"[{self.code}] {self.message}")


class InProcessClient:
    """Drive a ``StdioMCPServer`` without a subprocess: performs the initialize handshake, then
    exposes ``list_tools()`` and ``call(name, **args)`` that unwrap the MCP envelope to the tool's
    JSON result. This is exactly what an MCP client does over the wire, minus the pipe -- used by
    the reference driver and the tests."""

    def __init__(self, server: StdioMCPServer, client_name: str = "in-process"):
        self.server = server
        self._id = 0
        init = self._rpc("initialize", {
            "protocolVersion": _DEFAULT_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "0.1.0"},
        })
        self.server_instructions = init.get("instructions", "")
        self.protocol_version = init.get("protocolVersion")
        self._notify("notifications/initialized")

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        resp = self.server.handle({"jsonrpc": "2.0", "id": self._next_id(),
                                   "method": method, "params": params or {}})
        if resp is None:
            raise RuntimeError(f"{method} returned no response")
        if "error" in resp:
            raise RuntimeError(f"{method} JSON-RPC error: {resp['error']}")
        return resp["result"]

    def _notify(self, method: str, params: dict | None = None) -> None:
        self.server.handle({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list")["tools"]

    def call(self, tool, /, **arguments):
        """Call a tool and return its JSON result, or raise MCPToolError on a tool-level failure.

        ``tool`` is positional-only so a tool argument literally named ``name`` (e.g.
        describe_experiment) does not collide with the method parameter."""
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        data = json.loads(result["content"][0]["text"])
        if result.get("isError"):
            raise MCPToolError(data.get("error", {}))
        return data


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="gr-autopilot MCP stdio server (no SDK)")
    ap.add_argument("--log", default="", help="append the JSON-RPC transcript to this file")
    ap.add_argument("--backend", default="sim", choices=["sim"],
                    help="link backend (hardware backends are wired in the driver, not the server)")
    ap.add_argument("--es-n0-db", type=float, default=12.0,
                    help="hidden sim channel Es/N0 the agent must discover by measurement")
    args = ap.parse_args(argv)
    service = AutopilotService(channel={"es_n0_db": args.es_n0_db})
    StdioMCPServer(service, log_path=args.log or None).serve()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
