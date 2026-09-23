"""Entry point for the gr-autopilot MCP server.

    python -m gr_autopilot.mcp_server            # speak MCP over stdio (newline-delimited JSON-RPC)
    python -m gr_autopilot.mcp_server --log t.jsonl --es-n0-db 9

The server itself is a dependency-free JSON-RPC state machine in
``gr_autopilot.tools.mcp_stdio``; the tool set comes from ``gr_autopilot.tools.manifest`` (bound
to ``AutopilotService``). No `mcp` SDK is required -- installing one would pull a pip tree that
breaks the bench's system numpy/scipy (project memory) -- so this runs anywhere Python does.

Point an MCP client at it to let a real LLM drive the loop:
    claude mcp add gr-autopilot -- python -m gr_autopilot.mcp_server
"""
from __future__ import annotations

from gr_autopilot.tools.mcp_stdio import main

if __name__ == "__main__":
    raise SystemExit(main())
