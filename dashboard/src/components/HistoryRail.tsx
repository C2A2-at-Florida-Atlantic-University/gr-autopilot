import { useCallback, useEffect, useRef, useState } from "react"
import { LedgerTable } from "@/components/LedgerTable"
import { Button } from "@/components/ui/button"
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet"
import type { ExperimentInfo, LedgerRow } from "@/types"

/**
 * The edit ledger as a full-height rail on the right of the console.
 *
 * A time-ordered log wants height, not width: as a short panel under the plots it would cost
 * the instruments a quarter of the viewport and show only a few rows. Standing it up as a column
 * leaves that height to the instruments and lets the history run the way a log wants to run.
 *
 * The rail is present in every state, including when nothing has run: when the link is idle the
 * history is the only thing on screen that is still true.
 */
const MIN_W = 240
const MAX_W = 560
// Wide enough for the ledger's measurement columns — # / structure / BER / channel / SNR / EVM.
// The old 320 cleared only BER and SNR, so a reader watching the rail saw neither the EVM nor the
// channel a trial was taken on, and an interference episode looked like a column of unexplained
// error ratios. Anyone who wants the space back can still drag it down to MIN_W.
const DEFAULT_W = 440
const COLLAPSED_W = 36
// Bumped with the default: a width persisted from the narrower layout would otherwise pin every
// existing console to the columns this change exists to restore.
const KEY_W = "gra.ledger.width.v2"
const KEY_OPEN = "gra.ledger.open"

function load<T>(key: string, fallback: T, parse: (s: string) => T): T {
  try {
    const v = localStorage.getItem(key)
    return v === null ? fallback : parse(v)
  } catch {
    return fallback // private windows and blocked storage must not break the console
  }
}
function save(key: string, v: string) {
  try { localStorage.setItem(key, v) } catch { /* non-fatal */ }
}

/** Latest verdict, so the collapsed spine is never information-free. */
function latestTone(rows: LedgerRow[]): "good" | "bad" | "muted" {
  for (let i = rows.length - 1; i >= 0; i--) {
    const r = rows[i]
    if (r.verdict === "annotation") continue
    if (String(r.verdict).startsWith("aborted")) return "bad"
    const m = r.metrics ?? {}
    const ber = m.BER ?? m.ber
    if (typeof ber === "number") return ber > 0 ? "bad" : "good"
    return "muted"
  }
  return "muted"
}

export function HistoryRail({ rows, experiment }: { rows: LedgerRow[]; experiment: ExperimentInfo | null }) {
  const [width, setWidth] = useState(() => load(KEY_W, DEFAULT_W, (s) => Math.min(MAX_W, Math.max(MIN_W, +s || DEFAULT_W))))
  const [open, setOpen] = useState(() => load(KEY_OPEN, true, (s) => s !== "0"))
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.innerWidth < 1280)
  const [sheet, setSheet] = useState(false)
  const dragging = useRef(false)

  useEffect(() => {
    const on = () => setNarrow(window.innerWidth < 1280)
    window.addEventListener("resize", on)
    return () => window.removeEventListener("resize", on)
  }, [])
  useEffect(() => save(KEY_W, String(width)), [width])
  useEffect(() => save(KEY_OPEN, open ? "1" : "0"), [open])

  // Pointer drag. Width is measured from the right edge of the window so the handle tracks the
  // cursor exactly regardless of what else is on the page.
  const onDown = useCallback((e: React.PointerEvent) => {
    dragging.current = true
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
    e.preventDefault()
  }, [])
  const onMove = useCallback((e: React.PointerEvent) => {
    if (!dragging.current) return
    setWidth(Math.min(MAX_W, Math.max(MIN_W, window.innerWidth - e.clientX)))
  }, [])
  const onUp = useCallback((e: React.PointerEvent) => {
    dragging.current = false
    ;(e.target as HTMLElement).releasePointerCapture(e.pointerId)
  }, [])
  // Keyboard resize: the handle is a real control, not a mouse-only affordance.
  const onKey = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "ArrowLeft") { setWidth((w) => Math.min(MAX_W, w + 16)); e.preventDefault() }
    if (e.key === "ArrowRight") { setWidth((w) => Math.max(MIN_W, w - 16)); e.preventDefault() }
  }, [])

  const count = rows.filter((r) => r.verdict !== "annotation").length
  const tone = latestTone(rows)
  const toneCls = { good: "bg-good", bad: "bg-bad", muted: "bg-muted-foreground/50" }[tone]

  // Below 1280px the rail would steal width the plots need, so it becomes an overlay instead.
  if (narrow) {
    return (
      <>
        <Button variant="ghost" size="sm" onClick={() => setSheet(true)}
                className="fixed bottom-3 right-3 z-30 gap-2 border-border bg-card shadow-lg">
          <span className={`h-2 w-2 rounded-full ${toneCls}`} />
          <span className="font-mono text-[11px]">ledger · {count}</span>
        </Button>
        <Sheet open={sheet} onOpenChange={setSheet}>
          <SheetContent side="right" className="max-w-[440px] p-0">
            <SheetTitle className="sr-only">Edit ledger</SheetTitle>
            <div className="flex h-full min-h-0 flex-col p-3">
              <LedgerTable rows={rows} experiment={experiment} variant="rail" />
            </div>
          </SheetContent>
        </Sheet>
      </>
    )
  }

  if (!open) {
    return (
      <aside style={{ width: COLLAPSED_W }}
             className="flex shrink-0 flex-col items-center gap-3 border-l bg-muted/30 py-3">
        <button
          onClick={() => setOpen(true)}
          aria-label="Expand the edit ledger"
          className="rounded p-1 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-primary/60"
        >‹</button>
        <span className={`h-2 w-2 shrink-0 rounded-full ${toneCls}`} title="latest trial" />
        <span className="font-mono text-[10px] tabular-nums text-muted-foreground">{count}</span>
        <span className="mt-1 font-mono text-[10px] uppercase tracking-[2px] text-muted-foreground"
              style={{ writingMode: "vertical-rl" }}>ledger</span>
      </aside>
    )
  }

  return (
    <>
      {/* 2px rule, 9px hit area — a drag target you can actually catch. */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the edit ledger"
        tabIndex={0}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onKeyDown={onKey}
        className="group relative w-[9px] shrink-0 cursor-col-resize touch-none outline-none"
      >
        <span className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-border transition-colors
                         group-hover:bg-primary/60 group-focus-visible:bg-primary" />
      </div>
      <aside style={{ width }} className="flex min-h-0 shrink-0 flex-col border-l bg-background">
        <div className="flex items-center justify-between gap-2 px-3 py-2">
          <span className="font-mono text-[10px] uppercase tracking-[2px] text-muted-foreground">
            edit ledger
          </span>
          <button
            onClick={() => setOpen(false)}
            aria-label="Collapse the edit ledger"
            className="rounded px-1 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-primary/60"
          >›</button>
        </div>
        <div className="min-h-0 flex-1 px-2 pb-2">
          <LedgerTable rows={rows} experiment={experiment} variant="rail" width={width} />
        </div>
      </aside>
    </>
  )
}
