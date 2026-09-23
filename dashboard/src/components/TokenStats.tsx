import { useRef } from "react"
import { useCanvasDraw } from "@/lib/canvas"
import { ink } from "@/lib/ink"
import type { TokenStats as TS } from "@/types"

function fmt(n: number): string {
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k"
  return String(Math.round(n))
}

/** Tiny per-attempt token trend. */
function Sparkline({ values }: { values: number[] }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useCanvasDraw(
    ref,
    (ctx, w, h) => {
      ctx.clearRect(0, 0, w, h)
      if (values.length < 2) return
      const accent = ink().accent
      const max = Math.max(...values, 1)
      const pad = 2
      const X = (i: number) => pad + (i / (values.length - 1)) * (w - 2 * pad)
      const Y = (v: number) => h - pad - (v / max) * (h - 2 * pad)
      ctx.beginPath()
      ctx.moveTo(X(0), Y(values[0]))
      for (let i = 1; i < values.length; i++) ctx.lineTo(X(i), Y(values[i]))
      ctx.strokeStyle = accent
      ctx.lineWidth = 1.5
      ctx.stroke()
      const lx = X(values.length - 1)
      const ly = Y(values[values.length - 1])
      ctx.beginPath()
      ctx.arc(lx, ly, 2, 0, 2 * Math.PI)
      ctx.fillStyle = accent
      ctx.fill()
    },
    [values],
  )
  return <canvas ref={ref} className="block h-full w-full" />
}

/** Header chip: the token footprint the driving LLM read/emitted through the MCP tool surface —
 * overall total, tool calls, per-attempt average, and the per-attempt trend. A proxy, not the
 * client model's true tokenizer (the server can't see it). */
export function TokenStats({ tokens }: { tokens: TS }) {
  return (
    <div
      className="inline-flex items-center gap-2.5 rounded-full border bg-card px-3 py-1 font-mono text-[11px]"
      title={`≈ MCP tool-surface token footprint${tokens.estimated ? " (estimated ~4 chars/token)" : ""} · ${tokens.attempts} attempts`}
    >
      <span className="text-[9px] uppercase tracking-wider text-muted-foreground">tokens</span>
      <span className="font-semibold text-primary tabular-nums">{fmt(tokens.total)}</span>
      <span className="tabular-nums text-muted-foreground">{tokens.calls} calls</span>
      <span className="tabular-nums text-muted-foreground">{fmt(tokens.avg_per_attempt)}/try</span>
      {tokens.per_attempt.length > 1 && (
        <span className="h-3.5 w-14">
          <Sparkline values={tokens.per_attempt} />
        </span>
      )}
    </div>
  )
}
