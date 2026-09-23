import type { McpTool } from "@/types"

/**
 * A read-only look at the agent's tool surface, from the browser, over the same /mcp endpoint the
 * agent uses. The transport is streamable HTTP: a session is opened with `initialize`, carried in
 * the Mcp-Session-Id header, and closed with DELETE so the daemon does not accumulate one session
 * per page view. Responses may arrive as JSON or as one SSE `data:` line.
 */
const HDR = { "Content-Type": "application/json", Accept: "application/json, text/event-stream" }

async function rpc(body: unknown, sid?: string | null): Promise<{ json: unknown; sid: string | null }> {
  const r = await fetch("./mcp", {
    method: "POST",
    headers: { ...HDR, ...(sid ? { "Mcp-Session-Id": sid } : {}) },
    body: JSON.stringify(body),
  })
  const sidOut = r.headers.get("Mcp-Session-Id")
  const text = await r.text()
  let json: unknown = null
  if (text.trim().startsWith("{")) json = JSON.parse(text)
  else {
    const line = text.split("\n").find((l) => l.startsWith("data:"))
    if (line) json = JSON.parse(line.slice(5))
  }
  return { json, sid: sidOut }
}

export async function listTools(): Promise<McpTool[]> {
  const init = await rpc({
    jsonrpc: "2.0", id: 1, method: "initialize",
    params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "dashboard", version: "1" } },
  })
  const sid = init.sid
  await rpc({ jsonrpc: "2.0", method: "notifications/initialized" }, sid).catch(() => undefined)
  try {
    const res = await rpc({ jsonrpc: "2.0", id: 2, method: "tools/list" }, sid)
    const j = res.json as { result?: { tools?: McpTool[] }; error?: { message?: string } } | null
    if (j?.error) throw new Error(j.error.message ?? "tools/list refused")
    return j?.result?.tools ?? []
  } finally {
    if (sid) fetch("./mcp", { method: "DELETE", headers: { "Mcp-Session-Id": sid } }).catch(() => undefined)
  }
}
