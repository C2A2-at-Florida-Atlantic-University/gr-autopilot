import { useEffect, useState } from "react"
import { useJson } from "@/lib/useJson"
import { PAGES } from "@/lib/router"
import { PageHeader, Empty, Raw } from "@/components/PageHeader"
import { Card } from "@/components/ui/card"
import type { FlowgraphsPayload } from "@/types"
import { parseGrc, type Block, type Conn } from "@/lib/grc"

const P = PAGES.find((p) => p.id === "flowgraphs")!

/** Left-to-right layering by longest path from a source, like Companion's default reading order. */
function layout(blocks: Block[], conns: Conn[]) {
  const ids = blocks.map((b) => b.name)
  const preds: Record<string, string[]> = Object.fromEntries(ids.map((i) => [i, []]))
  for (const [s, , d] of conns) if (preds[d] && ids.includes(s)) preds[d].push(s)
  const depth: Record<string, number> = {}
  const dep = (n: string, seen: Set<string>): number => {
    if (depth[n] !== undefined) return depth[n]
    if (seen.has(n)) return 0
    seen.add(n)
    const d = preds[n].length ? 1 + Math.max(...preds[n].map((p) => dep(p, seen))) : 0
    depth[n] = d
    return d
  }
  ids.forEach((n) => dep(n, new Set()))
  const cols: Record<number, string[]> = {}
  ids.forEach((n) => { (cols[depth[n]] ??= []).push(n) })
  const pos: Record<string, { x: number; y: number }> = {}
  const CW = 190, RH = 58
  Object.entries(cols).forEach(([c, names]) => names.forEach((n, r) => { pos[n] = { x: 20 + Number(c) * CW, y: 20 + r * RH } }))
  const width = 40 + (Object.keys(cols).length) * CW
  const height = 40 + Math.max(...Object.values(cols).map((c) => c.length)) * RH
  return { pos, width, height }
}

function GrcView({ text }: { text: string }) {
  const g = parseGrc(text)
  if (g.blocks.length === 0) return <div className="text-[12px] text-muted-foreground">No blocks found in this file.</div>
  const { pos, width, height } = layout(g.blocks, g.conns)
  const BW = 160, BH = 40
  return (
    <div className="flex flex-col gap-2">
      {Object.keys(g.options).length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-muted-foreground">
          {["id", "title", "generate_options"].filter((k) => g.options[k]).map((k) => <span key={k}>{k} <span className="text-foreground">{g.options[k]}</span></span>)}
        </div>
      )}
      <div className="overflow-x-auto rounded-lg border bg-muted/40">
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="flowgraph blocks and connections">
          <defs>
            <marker id="fg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M0 0L10 5L0 10z" fill="var(--muted-foreground)" />
            </marker>
          </defs>
          {g.conns.map(([s, sp, d, dp], i) => {
            const a = pos[s], b = pos[d]
            if (!a || !b) return null
            const x0 = a.x + BW, y0 = a.y + BH / 2, x1 = b.x, y1 = b.y + BH / 2
            const c = Math.max(30, (x1 - x0) / 2)
            return (
              <g key={i}>
                <path d={`M${x0} ${y0} C${x0 + c} ${y0}, ${x1 - c} ${y1}, ${x1} ${y1}`} fill="none" stroke="var(--muted-foreground)" strokeWidth={1.4} markerEnd="url(#fg-arrow)" />
                <text x={x0 + 4} y={y0 - 4} fontSize={9} fontFamily="ui-monospace, monospace" fill="var(--muted-foreground)">{sp}</text>
                <text x={x1 - 4} y={y1 - 4} textAnchor="end" fontSize={9} fontFamily="ui-monospace, monospace" fill="var(--muted-foreground)">{dp}</text>
              </g>
            )
          })}
          {g.blocks.map((b) => {
            const p = pos[b.name]
            const src = !g.conns.some(([, , d]) => d === b.name), sink = !g.conns.some(([s]) => s === b.name)
            const accent = src ? "var(--primary)" : sink ? "var(--spectrum)" : "var(--border)"
            return (
              <g key={b.name}>
                <rect x={p.x} y={p.y} width={BW} height={BH} rx={6} fill="var(--card)" stroke={accent} strokeWidth={src || sink ? 1.5 : 1} />
                <text x={p.x + 8} y={p.y + 16} fontSize={11} fontWeight={600} fontFamily="ui-monospace, monospace" fill="var(--foreground)">{b.id || b.name}</text>
                <text x={p.x + 8} y={p.y + 30} fontSize={9.5} fontFamily="ui-monospace, monospace" fill="var(--muted-foreground)">{b.name}</text>
              </g>
            )
          })}
        </svg>
      </div>
      <div className="font-mono text-[11px] text-muted-foreground">{g.blocks.length} blocks · {g.conns.length} connections</div>
    </div>
  )
}

/**
 * Exported GNU Radio Companion files: listed, downloadable, and drawn. The drawing is a reading
 * of the .grc, not a screenshot — open the file in Companion for the real thing.
 */
export function FlowgraphsPage() {
  const q = useJson<FlowgraphsPayload>("./flowgraphs", 5000)
  const [sel, setSel] = useState<string | null>(null)
  const [text, setText] = useState<string | null>(null)
  const [showRaw, setShowRaw] = useState(false)
  const files = q.data?.flowgraphs ?? []
  const first = files[0] ?? null
  useEffect(() => { if (sel === null && first !== null) setSel(first) }, [first, sel])
  useEffect(() => {
    if (!sel) { setText(null); return }
    let alive = true
    fetch("./flowgraphs/" + encodeURIComponent(sel), { cache: "no-store" })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((t) => { if (alive) setText(t) })
      .catch((e) => { if (alive) setText(`# could not load: ${(e as Error).message}`) })
    return () => { alive = false }
  }, [sel])

  return (
    <div className="flex flex-col gap-3">
      <PageHeader title="Flowgraphs" endpoint={P.endpoint} hint="exported .grc files for this experiment — open with gnuradio-companion" at={q.at} error={q.error}>
        {q.data?.dir && <span className="opacity-60" title={q.data.dir}>{q.data.dir.split("/").slice(-3).join("/")}</span>}
      </PageHeader>
      {q.error && !q.data ? (
        <Empty><span className="text-bad">{q.error}</span> — this endpoint exists only when the daemon was started with <code className="font-mono">--ledger</code>.</Empty>
      ) : files.length === 0 ? (
        <Empty>No flowgraphs exported yet. Ask the agent to <code className="font-mono">export_flowgraph</code>, or run one of the <a href="#/prompts" className="text-primary">prompts</a>.</Empty>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-[220px_1fr]">
          <Card tier="tertiary" className="px-2 py-2">
            <div className="mb-1 px-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">{files.length} file{files.length === 1 ? "" : "s"}</div>
            {files.map((f) => (
              <button key={f} onClick={() => setSel(f)}
                      className={`block w-full rounded px-2 py-1 text-left font-mono text-[12px] ${sel === f ? "bg-primary/10 text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
                {f}
              </button>
            ))}
          </Card>
          <Card tier="secondary" className="px-3.5 py-3">
            {sel && (
              <div className="mb-2 flex flex-wrap items-center gap-3 font-mono text-[11px]">
                <span className="text-foreground">{sel}</span>
                <a href={"./flowgraphs/" + encodeURIComponent(sel)} download className="rounded border px-2 py-0.5 text-primary hover:border-primary/40">download .grc</a>
                <button onClick={() => setShowRaw((v) => !v)} className="rounded border px-2 py-0.5 text-muted-foreground hover:text-foreground">{showRaw ? "drawing" : "source"}</button>
                <span className="text-muted-foreground">gnuradio-companion {q.data?.dir}/{sel}</span>
              </div>
            )}
            {text === null ? <div className="text-[12px] text-muted-foreground">loading…</div>
              : showRaw ? <pre className="max-h-[60vh] overflow-auto rounded-lg border bg-muted/60 p-3 font-mono text-[11px] leading-relaxed">{text}</pre>
              : <GrcView text={text} />}
          </Card>
        </div>
      )}
      <p className="max-w-[80ch] text-[11.5px] text-muted-foreground">
        Today's export renders the agent's specification. In simulation that is the graph that ran; on the radios the graded link
        still executes a fixed pipeline, so treat a hardware export as "declared", not "as run", until the flowgraph-first path lands.
      </p>
      {q.data && <Raw data={q.data} />}
    </div>
  )
}
