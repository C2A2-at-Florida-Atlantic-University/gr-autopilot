import { useRef } from "react"
import { useCanvasDraw } from "@/lib/canvas"
import { alpha, ink, MONO_SM } from "@/lib/ink"
import { placeLabels } from "@/lib/axis"
import type { OccupancyChannel } from "@/types"

// Colours are read from the tokens at draw time, not frozen at module load: a module-scope
// constant cannot see a theme or density change.

/** Channel-occupancy bar chart: the monitor's energy-detection sweep across candidate center
 * frequencies (link silent). Occupied channels read as tall red bars, clear channels as short
 * dim-green ones, and the channel the link is currently tuned to is ringed in cyan — so the
 * jammer-avoidance hop (link moving off a red bar onto a green one) is legible at a glance. */
export function Occupancy({
  channels,
  currentFreqHz,
  thresholdDb = 3,
}: {
  channels: OccupancyChannel[]
  currentFreqHz?: number
  thresholdDb?: number
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  useCanvasDraw(
    ref,
    (ctx, w, h) => {
      const I = ink()
      const BAD = I.bad, GOOD = I.good, LINK = I.trace[1]
      const AXIS = I.grid, GRID = I.label, MUTED = I.dim
      ctx.clearRect(0, 0, w, h)
      const n = channels.length
      if (!n) return

      const padT = 28
      const padB = 24
      const padX = 12
      const baseY = h - padB
      const topY = padT
      const maxP = Math.max(thresholdDb * 2, ...channels.map((c) => c.power_db))
      const ceil = Math.max(12, maxP * 1.18)
      const Y = (db: number) => baseY - (Math.max(0, Math.min(db, ceil)) / ceil) * (baseY - topY)
      const slot = (w - 2 * padX) / n
      const barW = Math.min(48, slot * 0.5)

      // baseline
      ctx.strokeStyle = AXIS
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(padX, baseY)
      ctx.lineTo(w - padX, baseY)
      ctx.stroke()

      // detection threshold (dashed) — the +3 dB line the backend uses to call a channel occupied
      const ty = Y(thresholdDb)
      ctx.setLineDash([4, 4])
      ctx.strokeStyle = GRID
      ctx.beginPath()
      ctx.moveTo(padX, ty)
      ctx.lineTo(w - padX, ty)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = MUTED
      ctx.font = MONO_SM
      ctx.textAlign = "right"
      ctx.textBaseline = "alphabetic"
      ctx.fillText(`detect +${thresholdDb} dB`, w - padX, ty - 4)

      for (let i = 0; i < n; i++) {
        const c = channels[i]
        const cx = padX + slot * (i + 0.5)
        const x = cx - barW / 2
        const yTop = Y(c.power_db)
        const isLink = currentFreqHz != null && Math.abs(c.center_freq_hz - currentFreqHz) < 1e3

        // bar fill: alarming red for the jammer, calm dim green for a clear channel
        ctx.fillStyle = c.occupied ? alpha(BAD, 0.82) : alpha(GOOD, 0.38)
        ctx.fillRect(x, yTop, barW, baseY - yTop)
        ctx.fillStyle = c.occupied ? BAD : GOOD // bright cap
        ctx.fillRect(x, yTop, barW, 2)

        // current-link channel: cyan ring + a pointer + a label
        if (isLink) {
          ctx.strokeStyle = LINK
          ctx.lineWidth = 1.5
          ctx.strokeRect(x - 1.5, yTop - 1.5, barW + 3, baseY - yTop + 1.5)
          ctx.fillStyle = LINK
          ctx.beginPath()
          ctx.moveTo(cx - 5, yTop - 13)
          ctx.lineTo(cx + 5, yTop - 13)
          ctx.lineTo(cx, yTop - 6)
          ctx.closePath()
          ctx.fill()
        }

        // power value above the bar
        ctx.fillStyle = c.occupied ? BAD : I.dim
        ctx.font = "600 10px ui-monospace, monospace"
        ctx.textAlign = "center"
        const v = `${c.power_db >= 0 ? "+" : ""}${c.power_db.toFixed(1)}`
        ctx.fillText(v, cx, isLink ? yTop - 18 : yTop - 6)

      }

      // ---- frequency labels below the baseline -------------------------------------------------
      // Drawn in a pass of their own so they can be laid out against each other: a wide sweep puts
      // more candidates on the axis than there is room to name, and overlapping text under a bar
      // chart is worse than no text. The link's own channel is named first and never dropped.
      ctx.textAlign = "center"
      const labelled = channels.map((c, i) => ({
        c,
        x: padX + slot * (i + 0.5),
        isLink: currentFreqHz != null && Math.abs(c.center_freq_hz - currentFreqHz) < 1e3,
        text: (c.center_freq_hz / 1e6).toFixed(1),
      }))
      // The unit sits in the bottom-right corner; reserve it so a tick is never written through it.
      ctx.font = MONO_SM
      const unitW = ctx.measureText("MHz").width
      const shown = placeLabels(
        labelled,
        (t) => t.x,
        (t) => { ctx.font = `${t.isLink ? "600 " : ""}10px ui-monospace, monospace`; return ctx.measureText(t.text).width / 2 },
        (t) => (t.isLink ? 2 : t.c.occupied ? 1 : 0),
        6,
        [[w - padX - unitW - 8, w]],
      )
      for (const t of shown) {
        ctx.fillStyle = t.isLink ? LINK : MUTED
        ctx.font = `${t.isLink ? "600 " : ""}10px ui-monospace, monospace`
        ctx.fillText(t.text, t.x, baseY + 14)
      }
      ctx.fillStyle = MUTED
      ctx.font = MONO_SM
      ctx.textAlign = "right"
      ctx.fillText("MHz", w - padX, baseY + 14)
    },
    [channels, currentFreqHz, thresholdDb],
  )
  return <canvas ref={ref} className="block h-full w-full" />
}
