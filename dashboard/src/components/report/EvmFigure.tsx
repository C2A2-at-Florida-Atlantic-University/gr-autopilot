import { useRef } from "react"
import { useCanvasDraw } from "@/lib/canvas"
import { alpha, ink, MONO_BOLD, MONO_SM } from "@/lib/ink"
import type { Rung } from "@/lib/report"

/**
 * Error-vector magnitude per rung: mean, one standard deviation, and the full observed range.
 *
 * This is the companion question to the ladder. The ladder says where the link stopped meeting
 * its target; this says whether it stopped because the constellation ran out of room between
 * decision boundaries, or because the error vector itself grew. A rung whose EVM is unchanged
 * from the rung below it but whose BER jumped did not run into noise — it ran into geometry.
 */
export function EvmFigure({ rungs }: { rungs: Rung[] }) {
  const ref = useRef<HTMLCanvasElement>(null)

  useCanvasDraw(
    ref,
    (ctx, w, h) => {
      const I = ink()
      ctx.clearRect(0, 0, w, h)
      const withEvm = rungs.filter((r) => r.verdictAgg.evmMean !== null)
      if (!withEvm.length) {
        ctx.fillStyle = I.label
        ctx.font = MONO_SM
        ctx.textAlign = "center"
        ctx.fillText("no EVM recorded", w / 2, h / 2)
        return
      }

      const pad = { t: 14, r: 12, b: 34, l: 48 }
      const plotW = w - pad.l - pad.r
      const plotH = h - pad.t - pad.b

      const maxEvm = Math.max(...withEvm.map((r) => r.verdictAgg.evmMax ?? 0), 10)
      const top = Math.ceil(maxEvm / 10) * 10
      const Y = (v: number) => pad.t + plotH - (v / top) * plotH

      // gridlines every 10 %, labelled
      const step = top > 80 ? 25 : top > 40 ? 20 : 10
      ctx.font = MONO_SM
      ctx.textAlign = "right"
      for (let v = 0; v <= top + 0.001; v += step) {
        const y = Y(v)
        ctx.strokeStyle = I.grid
        ctx.lineWidth = 1
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke()
        ctx.fillStyle = I.label
        ctx.fillText(String(v), pad.l - 6, y + 3)
      }

      const slot = plotW / withEvm.length
      const boxW = Math.min(30, slot * 0.34)

      withEvm.forEach((r, i) => {
        const cx = pad.l + slot * (i + 0.5)
        const a = r.verdictAgg
        const tone = r.meets === true ? I.good : I.bad
        const mean = a.evmMean as number
        const sd = a.evmSd ?? 0
        const lo = a.evmMin ?? mean
        const hi = a.evmMax ?? mean

        // whisker: the full observed range, which on a bimodal rung is the whole story
        ctx.strokeStyle = alpha(tone, 0.55)
        ctx.lineWidth = 1.2
        ctx.beginPath(); ctx.moveTo(cx, Y(hi)); ctx.lineTo(cx, Y(lo)); ctx.stroke()
        for (const v of [lo, hi]) {
          ctx.beginPath(); ctx.moveTo(cx - 5, Y(v)); ctx.lineTo(cx + 5, Y(v)); ctx.stroke()
        }

        // ± one sd box
        const yTop = Y(Math.min(top, mean + sd))
        const yBot = Y(Math.max(0, mean - sd))
        ctx.fillStyle = alpha(tone, 0.2)
        ctx.fillRect(cx - boxW / 2, yTop, boxW, Math.max(1, yBot - yTop))
        ctx.strokeStyle = alpha(tone, 0.7)
        ctx.lineWidth = 1
        ctx.strokeRect(cx - boxW / 2, yTop, boxW, Math.max(1, yBot - yTop))

        // the mean itself
        ctx.strokeStyle = tone
        ctx.lineWidth = 2
        ctx.beginPath(); ctx.moveTo(cx - boxW / 2, Y(mean)); ctx.lineTo(cx + boxW / 2, Y(mean)); ctx.stroke()

        ctx.fillStyle = I.dim
        ctx.font = MONO_SM
        ctx.textAlign = "center"
        ctx.fillText(mean.toFixed(1), cx, Y(mean) - 6)

        ctx.fillStyle = r.meets === true ? I.ink : I.dim
        ctx.font = MONO_BOLD
        ctx.fillText(r.label, cx, h - pad.b + 14)
      })

      ctx.save()
      ctx.translate(11, pad.t + plotH / 2)
      ctx.rotate(-Math.PI / 2)
      ctx.fillStyle = I.label
      ctx.font = MONO_SM
      ctx.textAlign = "center"
      ctx.fillText("EVM %  (mean, ±1 sd, range)", 0, 0)
      ctx.restore()
    },
    [rungs],
  )

  return <canvas ref={ref} className="block h-full w-full" />
}
