#!/usr/bin/env python3
"""A hardware-backed MCP agent server: let a REAL LLM drive build->run->measure->see->adapt on radios.

Holds a PERSISTENT PlutoBackend-backed AutopilotService (the gr-iio flowgraph is built once and reused)
and exposes it over HTTP so a real MCP client -- a frontier LLM -- can drive the loop live on the two
Plutos, one tool call at a time, reacting to genuine BER/EVM/constellation measurements. The framework
(operator) sets the hidden condition on a SEPARATE endpoint; the agent never sees it (the tools do not
leak the SNR), so the agent's decisions rest on measurements alone.

  POST /agent    {"tool":"run_flowgraph","arguments":{...}}  -> the MCP tool result (no hidden leak)
  POST /operator {"tx_atten_db":55,"jammer":true,"jammer_freq":2.401e9}  -> framework hidden condition
  GET  /                                                       -> status

diagnose_signal(render_path=...) writes the REAL constellation PNG to <workdir>/shots/ for the client
to read. gr-iio teardown hangs, so shutdown is os._exit.

Run:  python scripts/hw_agent_server.py --port 8770 --tx-atten 40
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STATE: dict = {}


def _reply(h, code, obj):
    body = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 64 * 1024:                       # cap the body so a local client can't OOM us
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _cross_origin(self) -> bool:
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs and sfs not in ("same-origin", "same-site", "none"):
            return True
        o = self.headers.get("Origin")
        return bool(o and not o.startswith(("http://127.0.0.1", "http://localhost")))

    def do_GET(self):
        if self.path == "/":
            st = STATE
            # Deliberately does NOT report jammer state: interferer-present is hidden ground truth
            # the agent must infer by sensing, and this status endpoint is reachable from the same
            # transport as /agent. Leaking it here would let the agent read the condition directly.
            _reply(self, 200, {"backend": "pluto", "tools": len(st["cli"].list_tools()),
                               "shots_dir": str(st["shots"])})
        else:
            _reply(self, 404, {"error": "not found"})

    def do_POST(self):
        st = STATE
        if self._cross_origin():                # CSRF guard: no cross-site browser POST
            return _reply(self, 403, {"error": "cross-origin blocked"})
        try:
            req = self._body()
        except Exception as exc:
            return _reply(self, 400, {"error": f"bad request: {exc}"})

        if self.path == "/agent":
            tool = req.get("tool")
            args = req.get("arguments") or {}
            if tool == "diagnose_signal" and "render_path" not in args:  # auto-render for the client
                args["render_path"] = str(st["shots"] / f"c{st['n']}.png")
                st["n"] += 1
            try:
                from gr_autopilot.tools import MCPToolError
                data = st["cli"].call(tool, **args)
                return _reply(self, 200, {"ok": True, "result": data})
            except MCPToolError as exc:
                return _reply(self, 200, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
            except Exception as exc:
                return _reply(self, 200, {"ok": False, "error": {"code": "internal", "message": str(exc)}})

        if self.path == "/operator":
            # framework sets the HIDDEN condition. This is the operator surface — the split from
            # /agent is enforced by a token the operator holds and the agent never sees (not by URL
            # convention), so an agent that can POST cannot mutate the condition to ease its own grade.
            if self.headers.get("X-Operator-Token") != st["operator_token"]:
                return _reply(self, 403, {"error": "operator token required"})
            if req.get("tx_atten_db") is not None:
                st["svc"].set_channel(tx_atten_db=float(req["tx_atten_db"]))
            if "jammer" in req:
                _set_jammer(st, bool(req["jammer"]), req.get("jammer_freq"), req.get("if_gain", 20))
            return _reply(self, 200, {"ok": True})

        _reply(self, 404, {"error": "not found"})


def _set_jammer(st, on, freq, if_gain):
    from gr_autopilot.hardware.interferer import HackRFInterferer
    if st.get("jammer") is not None:
        st["jammer"].close()             # full teardown (stop + remove tempdir), not just stop()
        st["jammer"] = None
    if on:
        j = HackRFInterferer(center_freq_hz=float(freq or 2.4e9), if_gain=int(if_gain), kind="cw")
        j.start()
        st["jammer"] = j


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--tx-atten", type=float, default=40.0, help="initial hidden atten (dB)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--operator-token", default="",
                    help="token required on POST /operator (auto-generated if empty). The agent "
                         "driving /agent never receives it — this is the integrity boundary.")
    args = ap.parse_args()
    operator_token = args.operator_token or secrets.token_hex(16)

    try:
        from gr_autopilot.link.pluto import PlutoBackend
        from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer
    except Exception as exc:  # pragma: no cover
        print(f"cannot import hardware backends ({exc})", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="gr_autopilot_hwagent_"))
    shots = workdir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0,
                      rx_gain_db=args.rx_gain, sync_mode="zc")
    svc = AutopilotService(backend=be, channel={"tx_atten_db": args.tx_atten},
                           ledger_path=workdir / "s.jsonl")
    srv = StdioMCPServer(svc)
    cli = InProcessClient(srv)
    STATE.update({"svc": svc, "cli": cli, "be": be, "shots": shots, "n": 0, "jammer": None,
                  "operator_token": operator_token})

    httpd = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"hw agent server on http://127.0.0.1:{args.port}  ({len(cli.list_tools())} MCP tools)")
    print(f"shots -> {shots}")
    print(f"operator token (POST /operator needs 'X-Operator-Token'): {operator_token}")
    print(f"initial hidden tx_atten={args.tx_atten} dB (agent will not be told)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if STATE.get("jammer") is not None:
            STATE["jammer"].stop()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
