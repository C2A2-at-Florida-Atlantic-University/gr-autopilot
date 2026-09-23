import { useEffect, useRef, useState } from "react"
import { CHIP } from "@/components/Verdicts"
import { fmtCentreCompact } from "@/lib/axis"
import type { LinkState } from "@/lib/link"

/**
 * Where the link is tuned, and what the last sweep found there.
 *
 * Frequency avoidance was the one thing this console could not show. A retune changes no plot:
 * the PSD and the waterfall are drawn in offsets around whatever the centre happens to be, so
 * moving the link 10 MHz produced an identical-looking picture and the operator had to read the
 * agent's prose to learn the band had changed underneath them. The channel is a measurement like
 * any other and it belongs on the page as a number.
 *
 * The strip flashes for a few seconds when the number changes. That is the moment worth catching
 * — it is over in one poll otherwise — and it is deliberately a transient: a permanently lit
 * "RETUNED" badge would say nothing about when.
 */
const HOLD_MS = 9000

export function ChannelStrip({ link }: { link: LinkState }) {
  const { freqHz, lastRetune, lastSense, interferer } = link
  const [flash, setFlash] = useState(false)
  const seen = useRef<number | null>(null)

  useEffect(() => {
    if (freqHz == null) return
    // First sight of a frequency is not a retune — the page may have loaded mid-experiment.
    if (seen.current === null) { seen.current = freqHz; return }
    if (Math.abs(seen.current - freqHz) < 1e3) return
    seen.current = freqHz
    setFlash(true)
    const id = window.setTimeout(() => setFlash(false), HOLD_MS)
    return () => window.clearTimeout(id)
  }, [freqHz])

  if (freqHz == null && !lastSense) return null

  const busy = lastSense?.channels.filter((c) => c.occupied) ?? []
  const clear = lastSense?.channels.filter((c) => !c.occupied) ?? []
  // The margin that matters is the one between the occupied channel and the quietest clear one:
  // a jammer is only "standing above its neighbours" relative to something.
  const strongestBusy = busy.length ? Math.max(...busy.map((c) => c.power_db)) : null
  const quietestClear = clear.length ? Math.min(...clear.map((c) => c.power_db)) : null
  const separation = strongestBusy !== null && quietestClear !== null
    ? strongestBusy - quietestClear
    : null

  return (
    <div className={`flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border px-3 py-2 text-[11px] transition-colors duration-500 ${
      flash ? "border-primary/60 bg-primary/10" : "border-border bg-muted/40"}`}>
      <span className="uppercase tracking-wider text-muted-foreground">channel</span>
      <span className={`font-mono text-[13px] font-semibold tabular-nums ${flash ? "text-primary" : "text-foreground"}`}>
        {freqHz != null ? fmtCentreCompact(freqHz) : "—"}
      </span>

      {flash && lastRetune && (
        <span className={`${CHIP} bg-primary/15 text-primary`}>
          retuned{lastRetune.fromHz != null ? ` from ${fmtCentreCompact(lastRetune.fromHz)}` : ""}
        </span>
      )}
      {!flash && lastRetune && (
        <span className="text-muted-foreground">
          moved at #{lastRetune.iteration}
          {lastRetune.fromHz != null ? ` from ${fmtCentreCompact(lastRetune.fromHz)}` : ""}
        </span>
      )}

      {interferer && (
        <span className={`${CHIP} bg-bad/15 text-bad`}>
          {interferer.source === "sweep"
            ? `occupied +${interferer.marginDb.toFixed(1)} dB`
            : `spur +${interferer.marginDb.toFixed(1)} dB`}
        </span>
      )}

      {lastSense && (
        <span className="ml-auto text-muted-foreground">
          sweep #{lastSense.iteration} · {lastSense.channels.length} channels ·{" "}
          {busy.length === 0
            ? "none occupied"
            : `${busy.length} occupied${separation !== null ? `, ${separation.toFixed(1)} dB over the quietest clear one` : ""}`}
        </span>
      )}
    </div>
  )
}
