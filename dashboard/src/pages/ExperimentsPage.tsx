import { useJson } from "@/lib/useJson"
import { PAGES } from "@/lib/router"
import { PageHeader, Empty, Raw } from "@/components/PageHeader"
import { Button } from "@/components/ui/button"
import { experimentAction } from "@/lib/experiments"
import { openExperimentDialog } from "@/App"
import type { ExperimentsPayload } from "@/types"

const P = PAGES.find((p) => p.id === "experiments")!

function when(s: number | null | undefined) {
  return s == null ? "—" : new Date(s * 1000).toLocaleString()
}

/**
 * Every experiment under the runs directory, one row each, with the selected one marked. Open
 * switches to it (which resets the session and the radios — the toast says exactly what was
 * cleared); New starts another. The same verbs the agent has over MCP.
 */
export function ExperimentsPage() {
  const q = useJson<ExperimentsPayload>("./experiments", 3000)
  const rows = q.data?.experiments ?? []
  const current = q.data?.current ?? null
  return (
    <div className="flex flex-col gap-3">
      <PageHeader title="Experiments" endpoint={P.endpoint} hint="what has been run, and what each run produced" at={q.at} error={q.error}>
        {q.data?.runs_dir && <span className="opacity-60" title={q.data.runs_dir}>runs dir {q.data.runs_dir.split("/").slice(-2).join("/")}</span>}
        <Button size="sm" variant="primary" onClick={openExperimentDialog}>New experiment</Button>
      </PageHeader>
      {q.error && !q.data ? (
        <Empty><span className="text-bad">{q.error}</span></Empty>
      ) : rows.length === 0 ? (
        <Empty>No experiments under the runs directory yet — <button className="text-primary" onClick={openExperimentDialog}>start one</button>.</Empty>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-card">
          <table className="w-full text-[12px]">
            <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr>
                {["name", "goal", "backend", "started", "last activity", "iterations", ""].map((h) => (
                  <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="font-mono">
              {rows.map((e) => {
                const isCurrent = e.slug === current
                return (
                  <tr key={e.slug} className={`border-t border-border/60 align-top ${isCurrent ? "bg-primary/5" : ""}`}>
                    <td className="px-3 py-2 text-foreground">
                      {e.name}
                      <span className="ml-2 text-[10px] text-muted-foreground">{e.slug}</span>
                      {isCurrent && <span className="ml-2 rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-primary">current</span>}
                    </td>
                    <td className="max-w-[28ch] px-3 py-2 font-sans text-muted-foreground">{e.goal || <span className="italic opacity-70">no goal recorded</span>}</td>
                    <td className="px-3 py-2">{e.backend || "—"}</td>
                    <td className="px-3 py-2 whitespace-nowrap">{when(e.started_at)}</td>
                    <td className="px-3 py-2 whitespace-nowrap">{when(e.last_activity ?? e.started_at)}</td>
                    <td className="px-3 py-2 tabular-nums">{e.iterations}</td>
                    <td className="px-3 py-2">
                      {isCurrent
                        ? <Button size="sm" variant="ghost" onClick={openExperimentDialog}>rename</Button>
                        : <Button size="sm" variant="outline" onClick={() => experimentAction("switch", e.slug).then(() => q.refresh())}>Open</Button>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="max-w-[80ch] text-[11.5px] text-muted-foreground">
        Each experiment is a directory under the runs dir: <code className="font-mono">session.db</code> (iterations, artifacts),
        <code className="font-mono"> telemetry.json</code> (the last measurement) and <code className="font-mono">flowgraphs/</code> (exports).
        Opening a different one resets the session and the radios; renaming moves the directory and resets nothing.
      </p>
      {q.data && <Raw data={q.data} />}
    </div>
  )
}
