import { useEffect, useMemo, useRef } from "react"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { fmtBER } from "@/lib/utils"
import { fmtCentreCompact } from "@/lib/axis"
import { linkState } from "@/lib/link"
import type { ExperimentInfo, LedgerRow } from "@/types"

/** Rows the agent DECIDED rather than measured. They carry no metrics, so a table shaped for
 *  error ratios renders them as four empty cells and the decision disappears into the history —
 *  which is how a retune used to be invisible in the one record that contained it. */
const DECISION: Record<string, { glyph: string; tone: string }> = {
  retuned: { glyph: "↱", tone: "text-primary" },
  sensed: { glyph: "◌", tone: "text-muted-foreground" },
  diagnosed: { glyph: "◉", tone: "text-warn" },
  hopping: { glyph: "⇄", tone: "text-primary" },
  hopping_cleared: { glyph: "⇄", tone: "text-muted-foreground" },
  power: { glyph: "◐", tone: "text-muted-foreground" },
  conclusion: { glyph: "▸", tone: "text-muted-foreground" },
}

const num = (v: unknown): number | null => (typeof v === "number" && isFinite(v) ? v : null)

/**
 * The append-only edit ledger: what the agent changed, and what happened.
 *
 * Two variants. `card` is the old standalone panel (kept for any page that wants it inline);
 * `rail` is the full-height column in the shell, where the available columns follow the rail's
 * width. Columns are present or absent — never truncated mid-value, because half a BER is worse
 * than no BER.
 */
export function LedgerTable({ rows, experiment, variant = "card", width = 9999 }: {
  rows: LedgerRow[]
  experiment?: ExperimentInfo | null
  variant?: "card" | "rail"
  width?: number
}) {
  const viewport = useRef<HTMLDivElement>(null)
  const recent = variant === "rail" ? rows : rows.slice(-40)
  // Which channel each row was written on, replayed from the retunes in the same history.
  const freqAt = useMemo(() => linkState(rows).freqAt, [rows])

  // Pin to the newest row, like a log. Only the rail's own viewport scrolls, so a landing trial
  // never moves the plots beside it.
  useEffect(() => {
    const v = viewport.current
    if (v) v.scrollTop = v.scrollHeight
  }, [rows])

  // Column order is the order a reader needs them in: the error ratio, then what it was measured
  // AT (channel, SNR, EVM), then the bookkeeping. The gates are sized to the rendered content at
  // 12.5px mono with px-3 cells, and the rail's default width is set to clear all of them but the
  // verdict — a metric that is silently gated away reads as a metric that was never recorded.
  const showSnr = width >= 300
  const showFreq = width >= 350
  const showEvm = width >= 405
  // Last to appear: a decision row prints its own verdict as a glyph and a sentence across the
  // full width, so this column only ever restates what a measurement row already says ("run").
  const showVerdict = width >= 500

  const empty = (
    <div className="px-4 py-6 text-center text-[12px] text-muted-foreground">
      No iterations yet — the history fills in as the agent calls{" "}
      <code className="font-mono text-primary">run_flowgraph</code>.
    </div>
  )

  const body = (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>#</TableHead>
          <TableHead>structure</TableHead>
          <TableHead>BER</TableHead>
          {showFreq && <TableHead>chan/MHz</TableHead>}
          {showSnr && <TableHead>SNR</TableHead>}
          {showEvm && <TableHead>EVM</TableHead>}
          {showVerdict && <TableHead>verdict</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {recent.map((r, i) => {
          const m = r.metrics ?? {}
          const ber = m.BER ?? m.ber
          const snr = m.SNR_est ?? m.snr_db
          const evm = m.EVM ?? m.evm_pct
          const note = r.verdict === "annotation"
          const aborted = String(r.verdict).startsWith("aborted")
          const decision = DECISION[String(r.verdict)]
          const cols = 3 + (showFreq ? 1 : 0) + (showSnr ? 1 : 0) + (showEvm ? 1 : 0) + (showVerdict ? 1 : 0)

          // A decision spans the whole width and keeps its own sentence. At the rail's default
          // width there is no room for a channel column, and a retune is precisely the row that
          // must survive that: this is the line that says the link moved, and to where.
          if (decision) {
            const to = num((r.params as Record<string, unknown> | undefined)?.center_freq_hz)
            return (
              <TableRow key={`${r.iteration}-${i}`} className="border-dashed hover:bg-transparent">
                <TableCell className="tabular-nums text-muted-foreground/70">{r.iteration}</TableCell>
                <TableCell colSpan={cols - 1} className={`font-medium ${decision.tone}`}>
                  <span className="mr-1.5">{decision.glyph}</span>
                  {r.verdict === "retuned" && to !== null
                    ? <>retuned to <span className="font-mono tabular-nums">{fmtCentreCompact(to)}</span>
                        {num((r.params as Record<string, unknown>)?.from_hz) !== null && (
                          <span className="text-muted-foreground">
                            {" "}from {fmtCentreCompact(num((r.params as Record<string, unknown>).from_hz)!)}
                          </span>
                        )}</>
                    : r.edit_description}
                </TableCell>
              </TableRow>
            )
          }

          const chan = freqAt.get(r.iteration)
          return (
            <TableRow key={`${r.iteration}-${i}`} className={note ? "text-muted-foreground/70" : undefined}>
              <TableCell className="tabular-nums">{r.iteration}</TableCell>
              <TableCell className="max-w-[14ch] truncate" title={r.structure_id || r.edit_description}>
                {note ? "note" : r.structure_id || r.edit_description}
              </TableCell>
              <TableCell className={typeof ber === "number" && ber > 0 ? "text-bad" : undefined}>
                {typeof ber === "number" ? fmtBER(ber) : aborted ? "—" : ""}
              </TableCell>
              {showFreq && (
                // Only a graded trial is stamped: a note or an aborted run measured no channel,
                // and a number here would attribute one to it. Trailing zeros are dropped so the
                // column costs four characters on a whole megahertz instead of eight.
                <TableCell className="tabular-nums text-muted-foreground">
                  {chan != null && !note && typeof ber === "number"
                    ? String(parseFloat((chan / 1e6).toFixed(3)))
                    : ""}
                </TableCell>
              )}
              {showSnr && <TableCell className="tabular-nums">{typeof snr === "number" ? snr.toFixed(1) : ""}</TableCell>}
              {showEvm && <TableCell className="tabular-nums">{typeof evm === "number" ? evm.toFixed(1) : ""}</TableCell>}
              {showVerdict && (
                <TableCell>
                  {aborted ? <Badge variant="bad">aborted</Badge> : note ? "" : r.verdict}
                </TableCell>
              )}
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )

  if (variant === "rail") {
    return (
      <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-lg border bg-muted/40">
        {experiment && (
          <div className="shrink-0 border-b px-3 py-2 font-mono text-[11px]">
            <div className="truncate text-foreground" title={experiment.name}>{experiment.name}</div>
            {experiment.backend && (
              <div className="truncate text-[10px] text-muted-foreground" title={experiment.backend}>
                {experiment.backend}
              </div>
            )}
          </div>
        )}
        {rows.length === 0 ? empty : (
          <ScrollArea viewportRef={viewport} className="min-h-0 flex-1">{body}</ScrollArea>
        )}
      </div>
    )
  }

  return (
    <Card tier="tertiary" className="overflow-hidden">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b px-4 py-3 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span>Edit ledger — what the agent changed, and what happened</span>
        {experiment && (
          <span className="font-mono normal-case tracking-normal">
            {experiment.name}
            {experiment.backend && <span className="text-muted-foreground/70"> · {experiment.backend}</span>}
          </span>
        )}
      </div>
      {rows.length === 0 ? empty : (
        <ScrollArea viewportRef={viewport} className="max-h-[250px]">{body}</ScrollArea>
      )}
    </Card>
  )
}
