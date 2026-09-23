import { useEffect, useState } from "react"
import { listTools } from "@/lib/mcp"
import { PAGES } from "@/lib/router"
import { PageHeader, Empty, Raw } from "@/components/PageHeader"
import { Card } from "@/components/ui/card"
import type { McpTool } from "@/types"

const P = PAGES.find((p) => p.id === "tools")!

const GROUPS: [string, RegExp][] = [
  ["Bench", /^(list_devices|probe_device|claim_device|get_rf_path_config|get_status)$/],
  ["Author & run", /^(list_blocks|describe_block|list_skills|describe_skill|build_flowgraph|validate|run_flowgraph|get_metrics)$/],
  ["Perceive", /^(capture_constellation|capture_spectrum|diagnose_signal)$/],
  ["Tune", /^(start_bo_run|get_bo_status|stop_bo_run|set_tx_power)$/],
  ["Spectrum", /^(sense_spectrum|set_center_freq|set_hop_plan|clear_hop_plan)$/],
  ["Experiments & provenance", /^(describe_experiment|list_experiments|read_edit_ledger|annotate_ledger|export_flowgraph|import_flowgraph)$/],
  ["Skills", /^(author_skill|promote_skill)$/],
]
const groupOf = (n: string) => GROUPS.find(([, re]) => re.test(n))?.[0] ?? "Other"

function Tool({ t }: { t: McpTool }) {
  const props = t.inputSchema?.properties ?? {}
  const req = new Set(t.inputSchema?.required ?? [])
  const first = (t.description ?? "").split(/\n\s*\n/)[0]
  return (
    <div className="border-t border-border/60 py-2 first:border-t-0">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className="font-mono text-[12.5px] font-semibold text-foreground">{t.name}</span>
        <span className="font-mono text-[10.5px] text-muted-foreground">
          {Object.keys(props).length === 0 ? "no arguments" : Object.entries(props).map(([k, v]) => `${k}${req.has(k) ? "*" : ""}${v.type ? `: ${v.type}` : ""}`).join("  ")}
        </span>
      </div>
      {first && <p className="mt-0.5 max-w-[90ch] text-[12px] text-muted-foreground">{first}</p>}
    </div>
  )
}

/**
 * The agent's tool surface, read over the same /mcp endpoint the agent uses. What is NOT here is
 * the point: the interferer, the hidden channel and the grader have no tool, so the operator can
 * confirm by inspection that the agent cannot reach them.
 */
export function ToolsPage() {
  const [tools, setTools] = useState<McpTool[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [at, setAt] = useState<number | null>(null)
  useEffect(() => {
    let alive = true
    listTools().then((t) => { if (alive) { setTools(t); setAt(Date.now()) } }).catch((e) => { if (alive) setErr((e as Error).message) })
    return () => { alive = false }
  }, [])
  const groups = Array.from(new Set([...GROUPS.map(([g]) => g), "Other"]))
  return (
    <div className="flex flex-col gap-3">
      <PageHeader title="Tools" endpoint={P.endpoint} hint={`the agent's tool surface${tools ? ` · ${tools.length} tools` : ""} — read-only; the agent calls these, you do not`} at={at} error={err} />
      {err && !tools ? <Empty><span className="text-bad">{err}</span> — this page reads <code className="font-mono">/mcp</code>, which accepts a browser only from the bench host itself (the DNS-rebinding guard). From another machine, use the ssh tunnel described in the docs.</Empty>
        : !tools ? <Empty>opening a session…</Empty>
        : (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {groups.map((g) => {
              const ts = tools.filter((t) => groupOf(t.name) === g)
              if (ts.length === 0) return null
              return (
                <Card key={g} tier="tertiary" className="px-3.5 py-2.5">
                  <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">{g} · {ts.length}</div>
                  {ts.map((t) => <Tool key={t.name} t={t} />)}
                </Card>
              )
            })}
          </div>
        )}
      <p className="max-w-[80ch] text-[11.5px] text-muted-foreground">
        Not on this list, by design: arming the interferer, <code className="font-mono">tx_atten_db</code>, <code className="font-mono">rx_gain_db</code>, and the grader.
        Those live on the <a href="#/operator" className="text-primary">Operator</a> page behind the token.
      </p>
      {tools && <Raw data={tools} />}
    </div>
  )
}
