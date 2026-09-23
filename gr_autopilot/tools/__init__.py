"""The MCP tool surface (spec §8) as a plain service layer plus a dependency-free MCP server.

``AutopilotService`` implements the ~22 tools of §8 wired to the registries, backends,
framework-owned scoring, BO inner loop, and edit ledger, with the scoring-integrity split
enforced. ``tool_manifest()`` names, documents, and JSON-schemas those methods as the single
source of truth for any adapter, and ``StdioMCPServer`` speaks MCP over stdio (newline-delimited
JSON-RPC, no `mcp` SDK) so a real LLM client can drive the loop. ``InProcessClient`` drives the
same server object without a pipe (tests + the reference driver). Everything is testable with no
SDK and no hardware.
"""
from gr_autopilot.tools.manifest import INSTRUCTIONS, Tool, tool_manifest
from gr_autopilot.tools.mcp_stdio import InProcessClient, MCPToolError, StdioMCPServer
from gr_autopilot.tools.service import AutopilotService, ToolError

__all__ = [
    "AutopilotService",
    "ToolError",
    "Tool",
    "tool_manifest",
    "INSTRUCTIONS",
    "StdioMCPServer",
    "InProcessClient",
    "MCPToolError",
]
