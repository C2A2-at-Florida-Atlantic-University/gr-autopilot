import { Badge } from "@/components/ui/badge"
import type { ActiveLoop } from "@/types"

/** Which loop is driving. `detail` is free text from the agent ("run 32qam_link via MCP tool
 *  call"), so it is capped to one line: the header is a strip of fixed-height chips, and one long
 *  sentence in it would wrap the whole row and push the plots down the page. */
export function ActiveLoopBadge({ loop }: { loop: ActiveLoop }) {
  const inner = loop.loop === "inner"
  return (
    <Badge variant={inner ? "warn" : "primary"} title={loop.detail || undefined}>
      <span className="inline-block h-2 w-2 shrink-0 animate-pulse rounded-full bg-current" />
      <span className="shrink-0">{inner ? "BO inner" : "LLM outer"}</span>
      {loop.detail ? <span className="max-w-[28ch] truncate opacity-80">· {loop.detail}</span> : null}
    </Badge>
  )
}
