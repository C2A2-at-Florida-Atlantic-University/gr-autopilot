import { useRef } from "react"
import { useCanvasDraw } from "@/lib/canvas"
import { alpha, ink, MONO_BOLD, MONO_SM } from "@/lib/ink"
import type { Rung } from "@/lib/report"

/**
 * The ladder: pooled error ratio per rung against the target, with every trial shown behind it.
 *
 * A bar chart of pooled ratios alone would hide the thing that decides what a ceiling means. A
 * rung that sits at 1e-1 because every trial sits at 1e-1 is noise-limited; a rung that sits at
 * 1e-1 because half its trials are at 1e-5 and half are at 4e-1 is an acquisition that sometimes
 * happens, and the second is not a ceiling in the same sense. So the individual trials are drawn
 * as ticks over the pooled bar, and a rung whose trials split into two clusters is marked.
 *
 * The zero lane below the lowest decade exists for the same reason it does on the trends plot: a
 * rung with no errors at all has no position on a log axis, and putting it at the floor would
 * assert a ratio that was never measured.
 */
export function LadderFigure({ rungs, target }: { rungs: Rung[]; target: number }) {
  const ref = useRef<HTMLCanvasElement>(null)

  useCanvasDraw(
    ref,
    (ctx, w, h) => {
      const I = ink()
      ctx.clearRect(0, 0, w, h)
      if (!rungs.length) {
        ctx.fillStyle = I.label
        ctx.font = MONO_SM
        ctx.textAlign = "center"
        ctx.fillText("no trials", w / 2, h / 2)
        return
      }

      const pad = { t: 16, r: 12, b: 34, l: 48 }
      const zoneH = 16                       // the zero lane, beneath the log axis
      const plotW = w - pad.l - pad.r
      const plotH = h - pad.t - pad.b - zoneH

      // Axis range: every finite positive ratio on the plot, plus the target, padded a decade
      // each way so nothing sits on an edge.
      const vals: number[] = [target]
      for (const r of rungs) {
        for (const row of r.rows) {
          const b = (row.metrics?.BER ?? row.metrics?.ber) as number | undefined
          if (typeof b === "number" && b > 0) vals.push(b)
        }
        if (r.verdictAgg.ber && r.verdictAgg.ber > 0) vals.push(r.verdictAgg.ber)
        if (r.verdictAgg.berHi && r.verdictAgg.berHi > 0) vals.push(r.verdictAgg.berHi)
      }
      const lo = Math.pow(10, Math.floor(Math.log10(Math.min(...vals))) - 0.2)
      const hi = Math.pow(10, Math.ceil(Math.log10(Math.max(...vals, 1))) + 0.1)
      const Y = (v: number) => {
        const t = (Math.log10(v) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo))
        return pad.t + plotH - t * plotH
      }
      const zeroY = pad.t + plotH + zoneH / 2

      // decade gridlines
      ctx.font = MONO_SM
      ctx.textAlign = "right"
      for (let e = Math.ceil(Math.log10(lo)); e <= Math.floor(Math.log10(hi)); e++) {
        const v = Math.pow(10, e)
        const y = Y(v)
        ctx.strokeStyle = I.grid
        ctx.lineWidth = 1
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke()
        ctx.fillStyle = I.label
        ctx.fillText(`1e${e}`, pad.l - 6, y + 3)
      }

      // the zero lane and its separator
      ctx.strokeStyle = I.grid
      ctx.setLineDash([2, 3])
      ctx.beginPath(); ctx.moveTo(pad.l, pad.t + plotH + 1); ctx.lineTo(w - pad.r, pad.t + plotH + 1); ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = I.label
      ctx.fillText("0", pad.l - 6, zeroY + 3)

      // the target line — the only horizontal rule that is a claim rather than a scale
      const ty = Y(target)
      ctx.strokeStyle = alpha(I.warn, 0.85)
      ctx.lineWidth = 1.4
      ctx.setLineDash([5, 3])
      ctx.beginPath(); ctx.moveTo(pad.l, ty); ctx.lineTo(w - pad.r, ty); ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = I.warn
      ctx.font = MONO_BOLD
      ctx.textAlign = "left"
      ctx.fillText(`target ${target.toExponential(0)}`, pad.l + 4, ty - 4)

      const slot = plotW / rungs.length
      const barW = Math.min(46, slot * 0.46)

      rungs.forEach((r, i) => {
        const cx = pad.l + slot * (i + 0.5)
        const a = r.verdictAgg
        const pass = r.meets === true
        const tone = pass ? I.good : I.bad

        // pooled bar: from the axis floor up to the pooled ratio. A rung with zero errors has no
        // bar at all — it has a bound, drawn as a capped marker in the zero lane instead.
        if (a.ber !== null && a.ber > 0) {
          const y = Y(a.ber)
          ctx.fillStyle = alpha(tone, 0.22)
          ctx.fillRect(cx - barW / 2, y, barW, pad.t + plotH - y)
          ctx.strokeStyle = tone
          ctx.lineWidth = 1.6
          ctx.beginPath(); ctx.moveTo(cx - barW / 2, y); ctx.lineTo(cx + barW / 2, y); ctx.stroke()
        } else if (a.errors === 0 && a.berHi) {
          // "no errors in N bits": a downward arrow from the bound into the zero lane.
          const y = Y(a.berHi)
          ctx.strokeStyle = I.good
          ctx.lineWidth = 1.6
          ctx.beginPath(); ctx.moveTo(cx - barW / 2, y); ctx.lineTo(cx + barW / 2, y); ctx.stroke()
          ctx.beginPath()
          ctx.moveTo(cx, y); ctx.lineTo(cx, zeroY - 5)
          ctx.stroke()
          ctx.beginPath()
          ctx.moveTo(cx - 3.5, zeroY - 8); ctx.lineTo(cx, zeroY - 3); ctx.lineTo(cx + 3.5, zeroY - 8)
          ctx.stroke()
        }

        // every trial, as a tick — the spread behind the pooled number
        for (const row of r.rows) {
          const b = (row.metrics?.BER ?? row.metrics?.ber) as number | undefined
          if (typeof b !== "number") continue
          const y = b > 0 ? Y(b) : zeroY
          ctx.strokeStyle = alpha(b <= target ? I.good : I.bad, 0.85)
          ctx.lineWidth = 1.2
          ctx.beginPath()
          ctx.moveTo(cx - barW / 2 - 5, y); ctx.lineTo(cx - barW / 2 + 1, y)
          ctx.stroke()
        }

        // a rung that splits into two clusters is not simply "bad"; say so on the figure
        if (r.bimodal) {
          ctx.fillStyle = I.warn
          ctx.font = MONO_BOLD
          ctx.textAlign = "center"
          ctx.fillText("bimodal", cx, pad.t - 5)
        }

        // rung label + bits/symbol
        ctx.fillStyle = pass ? I.ink : I.dim
        ctx.font = MONO_BOLD
        ctx.textAlign = "center"
        ctx.fillText(r.label, cx, h - pad.b + 14)
        if (r.bps !== null) {
          ctx.fillStyle = I.label
          ctx.font = MONO_SM
          ctx.fillText(`${r.bps} b/sym`, cx, h - pad.b + 25)
        }
      })

      // y-axis caption
      ctx.save()
      ctx.translate(11, pad.t + plotH / 2)
      ctx.rotate(-Math.PI / 2)
      ctx.fillStyle = I.label
      ctx.font = MONO_SM
      ctx.textAlign = "center"
      ctx.fillText("bit error ratio", 0, 0)
      ctx.restore()
    },
    [rungs, target],
  )

  return <canvas ref={ref} className="block h-full w-full" />
}
