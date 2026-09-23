import { useEffect, useRef, useState } from "react"
import { useTelemetry } from "@/lib/telemetry"
import { useRoute } from "@/lib/router"
import { HistoryRail } from "@/components/HistoryRail"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Toaster } from "sonner"
import type { ExperimentInfo } from "@/types"
import { ExperimentDialog } from "@/components/ExperimentDialog"
import { ActiveLoopBadge } from "@/components/ActiveLoopBadge"
import { TokenStats } from "@/components/TokenStats"
import { Nav } from "@/components/Nav"
import { Overview } from "@/pages/Overview"
import { fmtAge } from "@/lib/utils"
import { DevicesPage } from "@/pages/DevicesPage"
import { TopologyPage } from "@/pages/TopologyPage"
import { ExperimentsPage } from "@/pages/ExperimentsPage"
import { FlowgraphsPage } from "@/pages/FlowgraphsPage"
import { PromptsPage } from "@/pages/PromptsPage"
import { ToolsPage } from "@/pages/ToolsPage"
import { OperatorPage } from "@/pages/OperatorPage"
import { DocsPage } from "@/pages/DocsPage"
import { RunReportDialog } from "@/components/report/RunReport"
import { useRunCompletion } from "@/lib/useRunCompletion"
import { Glyph } from "@/components/Glyph"

/** LIVE / IDLE / OFFLINE — a claim about the AGENT, not about the web server being reachable.
 *  Only the live state pulses; stale data must never animate as though it were arriving. */
function StatusBadge({ liveness, ageS, step }: { liveness: "live" | "idle" | "offline"; ageS: number | null; step?: number }) {
  const style = {
    live: "border-good/40 bg-good/10 text-good",
    idle: "border-warn/40 bg-warn/10 text-warn",
    offline: "border-bad/40 bg-bad/10 text-bad",
  }[liveness]
  const label = { live: "LIVE", idle: "IDLE", offline: "OFFLINE" }[liveness]
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[11px] font-semibold ${style}`}>
      <span className={`h-2 w-2 rounded-full bg-current ${liveness === "live" ? "animate-pulse" : ""}`} />
      {label}
      {liveness === "live" && step !== undefined ? ` · step ${step}` : ""}
      {liveness === "idle" && ageS !== null ? ` · ${fmtAge(ageS)} ago` : ""}
    </span>
  )
}

/**
 * Which experiment the console is looking at.
 *
 * The name and nothing else. The backend string ("pluto radios (ip:192.168.2.1 tx, ...)") is a
 * device inventory, not an identity: it is the same on every experiment this bench runs, it is
 * already on the Devices and Topology pages and in the dialog this chip opens, and at that length
 * it wraps the whole header onto a second row. Whether the numbers came from radios or a model is
 * answered by the bench state block in the navigation rail, which is where device facts belong.
 */
function ExperimentChip({ experiment, connected, onOpen }: {
  experiment: ExperimentInfo | null; connected: boolean; onOpen: () => void
}) {
  if (!connected) return null
  if (!experiment) {
    // Nothing selected: the chip IS the call to action, and it is styled as a warning because
    // every measuring tool is refusing until this is answered.
    return (
      <button onClick={onOpen}
              className="inline-flex items-center gap-2 rounded-full border border-warn/50 bg-warn/10 px-2.5 py-1 font-mono text-[11px] font-semibold text-warn hover:bg-warn/20 focus-visible:ring-2 focus-visible:ring-warn/60">
        <span className="h-2 w-2 rounded-full bg-current" />
        no experiment — name one
      </button>
    )
  }
  return (
    <button onClick={onOpen} title="Rename, switch or start an experiment"
            className="inline-flex max-w-[42ch] items-center gap-2 rounded-full border bg-muted/40 px-2.5 py-1 font-mono text-[11px] hover:bg-muted focus-visible:ring-2 focus-visible:ring-primary/60">
      <span className="truncate font-semibold text-foreground">{experiment.name}</span>
      <span className="shrink-0 text-muted-foreground/70">▾</span>
    </button>
  )
}

/** Anywhere in the app can ask for the experiment dialog without prop-drilling through pages. */
export function openExperimentDialog() {
  window.dispatchEvent(new CustomEvent("gra:experiment-dialog"))
}

export default function App() {
  const tele = useTelemetry()
  const { page, sub, navigate } = useRoute()
  const s = tele.snapshot
  const [expOpen, setExpOpen] = useState(false)
  // The gate: live mode, daemon answering, nothing selected -> the dialog opens and stays open.
  const gate = tele.mode === "live" && tele.connected && tele.experiment === null
  // A finished run summarises itself -- once, when the agent declares it finished, never on a
  // guess about quiet. Suppressed while the experiment gate is up, because that dialog is a
  // question that has to be answered before anything else is worth reading.
  const run = useRunCompletion(tele)
  useEffect(() => {
    const on = () => setExpOpen(true)
    window.addEventListener("gra:experiment-dialog", on)
    return () => window.removeEventListener("gra:experiment-dialog", on)
  }, [])
  useEffect(() => { if (gate) setExpOpen(true) }, [gate])
  const main = useRef<HTMLElement>(null)

  // The scroll container is <main>, not the window, so changing page must reset ITS scroll --
  // otherwise arriving at a short page from halfway down a long one lands below the content.
  useEffect(() => { main.current?.scrollTo({ top: 0 }) }, [page, sub])

  const body =
    page === "devices" ? <DevicesPage tele={tele} />
    : page === "topology" ? <TopologyPage tele={tele} />
    : page === "experiments" ? <ExperimentsPage />
    : page === "flowgraphs" ? <FlowgraphsPage />
    : page === "prompts" ? <PromptsPage />
    : page === "tools" ? <ToolsPage />
    : page === "operator" ? <OperatorPage tele={tele} />
    : page === "docs" ? <DocsPage sub={sub} navigate={navigate} />
    : <Overview tele={tele} />

  return (
    // The app shell: a viewport-height column that does not scroll. The header and the navigation
    // stay put; only the content area moves, so the state band and the page list are readable
    // wherever you are in a long document.
    <TooltipProvider delayDuration={200}>
    <div className="flex h-dvh w-full flex-col overflow-hidden">
      {/* One row of chips that each say something about the RUN. The display preferences that used
          to sit here moved to the foot of the navigation rail; everything that is left earns its
          width, and every variable-length string in it is capped so the header cannot grow a
          second row and steal height from the plots. */}
      <header className="flex shrink-0 items-center gap-4 border-b bg-background px-4 py-2.5 lg:px-5">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-4 gap-y-2">
          <a href="#/overview" onClick={(e) => { e.preventDefault(); navigate("overview") }} className="font-mono text-[17px] font-semibold">
            gr-<span className="text-primary">autopilot</span>
            <span className="ml-3 border-l pl-3 text-[11px] uppercase tracking-[2px] text-muted-foreground">portal</span>
          </a>
          <ExperimentChip experiment={tele.experiment} connected={tele.connected} onOpen={() => setExpOpen(true)} />
          <ExperimentDialog open={expOpen || gate} onOpenChange={setExpOpen} current={tele.experiment} gate={gate} />
          {tele.mode === "live" ? (
            <StatusBadge liveness={tele.liveness} ageS={tele.ageS} step={s?.t} />
          ) : (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-warn/40 bg-warn/10 px-2.5 py-1 font-mono text-[11px] font-semibold text-warn">
              <span className="h-2 w-2 rounded-full bg-current" />
              REPLAY · recorded
            </span>
          )}
          {/* The agent's goal, one line only. Nothing is printed here when the agent is quiet:
              the IDLE badge two chips to the left already says so, and a long string here is what
              wraps the header onto a second row. `truncate` keeps any goal to one line for the
              same reason. */}
          <div className="min-w-0 flex-1 truncate text-[13.5px] text-muted-foreground"
               title={tele.liveness === "offline" ? undefined : (s?.goal ?? undefined)}>
            {tele.liveness === "offline"
              ? "offline — no telemetry server at /data"
              : (s?.goal ?? "waiting for the agent…")}
          </div>
          {/* The report fires once per run now, so a stray dismissal would otherwise lose it. */}
          {run.available && !run.open && (
            <button
              type="button"
              onClick={run.reopen}
              title="Re-open the report for the last completed run"
              className="inline-flex items-center gap-1.5 rounded-full border border-primary/40 bg-primary/10 px-2.5 py-1
                         font-mono text-[11px] font-semibold text-primary transition-colors hover:bg-primary/20
                         focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
            >
              <Glyph name="doc" size={11} />
              RUN REPORT
            </button>
          )}
          {s?.tokens && <TokenStats tokens={s.tokens} />}
          {s && <ActiveLoopBadge loop={s.active_loop} />}
        </div>
      </header>

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <Nav page={page} sub={sub} navigate={navigate} tele={tele} />
        {/* The only scroll container on the page. `overscroll-contain` stops a flick at the end of
            a long docs page from chaining out to the browser. */}
        <main
          ref={main}
          id="content"
          className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-y-auto overscroll-contain px-4 py-4 lg:px-5"
        >
          {body}
          <footer className="mt-auto pt-2 text-center text-[11px] text-muted-foreground">
            Constellation, BER &amp; occupancy are framework-graded measurements · PSD is representative ·{" "}
            the operator surface sets the scenario; the agent's structural control stays on the MCP/chat surface
          </footer>
        </main>
        {/* The history rail. Present on every page and in every state: when the link is idle,
            the ledger is the only thing on screen that is still true. */}
        <HistoryRail rows={tele.ledger} experiment={tele.experiment} />
      </div>
      <RunReportDialog report={run.report} open={run.open && !gate} onClose={run.dismiss} />
      <Toaster
        position="bottom-center"
        toastOptions={{
          className: "!bg-card !border !border-border !text-foreground !font-mono !text-[12.5px]",
        }}
      />
    </div>
    </TooltipProvider>
  )
}
