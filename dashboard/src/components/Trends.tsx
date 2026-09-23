import { useRef } from "react"
import { useCanvasDraw } from "@/lib/canvas"
import { alpha, ink, MONO, MONO_BOLD, MONO_SM } from "@/lib/ink"
import { decadeTicks, niceTicks } from "@/lib/axis"
import type { LedgerRow } from "@/types"

/**
 * BER, SNR and EVM against iteration.
 *
 * The console could show what the link is doing NOW and what it had done (a table), but not the
 * shape of the thing in between — whether a rung is holding, drifting, or was only ever one lucky
 * sample. These three traces come from the ledger, so they cover the whole experiment rather than
 * the lifetime of this browser tab, and they are the same numbers the table shows.
 *
 * BER is drawn on a log axis with an explicit ZERO LANE beneath the lowest decade. A zero error
 * count has no place on a log scale, and plotting it at the axis floor would state a measured
 * value that was never measured; the separate lane says "no errors in this trial" without
 * pretending to know how small the true ratio is.
 */
// `tone` names a token rather than holding a colour, so the series definition can live at module
// scope while the actual ink is resolved per draw.
type Series = { key: "BER" | "SNR" | "EVM"; label: string; unit: string; tone: "bad" | "t1" | "warn"; log?: boolean }

const SERIES: Series[] = [
  { key: "BER", label: "BER", unit: "", tone: "bad", log: true },
  { key: "SNR", label: "SNR", unit: "dB", tone: "t1" },
  { key: "EVM", label: "EVM", unit: "%", tone: "warn" },
]

function pick(r: LedgerRow, key: Series["key"]): number | undefined {
  const m = r.metrics ?? {}
  if (key === "BER") return m.BER ?? m.ber
  if (key === "SNR") return m.SNR_est ?? m.snr_db ?? m.SNR
  return m.EVM ?? m.evm_pct
}

export function Trends({ rows, targetBer }: { rows: LedgerRow[]; targetBer?: number }) {
  const ref = useRef<HTMLCanvasElement>(null)
  // Annotations carry no metrics; including them would put gaps in a line that is meant to be
  // one point per completed trial.
  const runs = rows.filter((r) => pick(r, "SNR") !== undefined || pick(r, "BER") !== undefined)

  useCanvasDraw(
    ref,
    (ctx, w, h) => {
      const I = ink()
      ctx.clearRect(0, 0, w, h)
      if (!runs.length) {
        ctx.fillStyle = I.label
        ctx.font = MONO
        ctx.textAlign = "center"
        ctx.fillText("no trials yet", w / 2, h / 2)
        return
      }
      const pad = { t: 10, b: 16, l: 42, r: 8 }
      const gap = 8
      const paneH = (h - pad.t - pad.b - gap * (SERIES.length - 1)) / SERIES.length
      const plotW = w - pad.l - pad.r
      const X = (i: number) => pad.l + (runs.length === 1 ? plotW / 2 : (i / (runs.length - 1)) * plotW)

      const toneOf = (t: Series["tone"]) => (t === "bad" ? I.bad : t === "warn" ? I.warn : I.trace[0])
      SERIES.forEach((s, si) => {
        const color = toneOf(s.tone)
        const top = pad.t + si * (paneH + gap)
        const bot = top + paneH
        const vals = runs.map((r) => pick(r, s.key))

        ctx.strokeStyle = I.grid
        ctx.lineWidth = 1
        ctx.strokeRect(pad.l, top, plotW, paneH)

        ctx.font = MONO_SM
        ctx.textBaseline = "middle"

        if (s.log) {
          // Log lane for non-zero BER, plus a dedicated zero lane at the bottom.
          const pos = vals.filter((v): v is number => typeof v === "number" && v > 0)
          const hi = Math.max(1e-2, ...pos)
          const lo = Math.min(1e-5, ...(pos.length ? pos : [1e-5]))
          const zoneH = paneH * 0.18
          const logBot = bot - zoneH
          const Y = (v: number) =>
            logBot - ((Math.log10(v) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo))) * (logBot - top)

          ctx.strokeStyle = I.grid
          ctx.fillStyle = I.label
          ctx.textAlign = "right"
          for (const d of decadeTicks(lo, hi)) {
            const y = Y(d)
            ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + plotW, y); ctx.stroke()
            ctx.fillText(`1e${Math.round(Math.log10(d))}`, pad.l - 4, y)
          }
          // the zero lane and its divider
          ctx.strokeStyle = I.grid
          ctx.setLineDash([2, 2])
          ctx.beginPath(); ctx.moveTo(pad.l, logBot); ctx.lineTo(pad.l + plotW, logBot); ctx.stroke()
          ctx.setLineDash([])
          ctx.fillStyle = I.label
          ctx.fillText("0", pad.l - 4, bot - zoneH / 2)

          if (targetBer) {
            const y = Y(targetBer)
            if (y > top && y < logBot) {
              ctx.strokeStyle = alpha(I.bad, 0.55)
              ctx.setLineDash([4, 3])
              ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + plotW, y); ctx.stroke()
              ctx.setLineDash([])
              ctx.fillStyle = alpha(I.bad, 0.8)
              ctx.textAlign = "left"
              ctx.fillText("target", pad.l + 3, y - 6)
            }
          }

          // Points, not a joined line: a run with zero errors and one with 1e-4 are different
          // KINDS of result, and a line between them would imply a continuum that is not there.
          vals.forEach((v, i) => {
            if (typeof v !== "number") return
            const y = v > 0 ? Y(Math.max(v, lo)) : bot - zoneH / 2
            ctx.fillStyle = v > 0 ? color : I.trace[0]
            ctx.beginPath(); ctx.arc(X(i), y, 2.1, 0, Math.PI * 2); ctx.fill()
          })
        } else {
          const nums = vals.filter((v): v is number => typeof v === "number")
          let lo = Math.min(...nums), hi = Math.max(...nums)
          if (hi - lo < 1e-9) { lo -= 1; hi += 1 }
          const m = (hi - lo) * 0.15
          lo -= m; hi += m
          const Y = (v: number) => bot - ((v - lo) / (hi - lo)) * paneH

          ctx.strokeStyle = I.grid
          ctx.fillStyle = I.label
          ctx.textAlign = "right"
          for (const tk of niceTicks(lo, hi, 3)) {
            const y = Y(tk)
            if (y < top - 1 || y > bot + 1) continue
            ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + plotW, y); ctx.stroke()
            ctx.fillText(tk.toFixed(Math.abs(tk) < 10 ? 1 : 0), pad.l - 4, y)
          }
          ctx.beginPath()
          let started = false
          vals.forEach((v, i) => {
            if (typeof v !== "number") return
            const x = X(i), y = Y(v)
            if (!started) { ctx.moveTo(x, y); started = true } else ctx.lineTo(x, y)
          })
          ctx.strokeStyle = color
          ctx.lineWidth = 1.5
          ctx.stroke()
          vals.forEach((v, i) => {
            if (typeof v !== "number") return
            ctx.fillStyle = color
            ctx.beginPath(); ctx.arc(X(i), Y(v), 1.8, 0, Math.PI * 2); ctx.fill()
          })
        }

        // series name + unit, inside the pane so it cannot be mistaken for an axis value
        ctx.textAlign = "left"
        ctx.fillStyle = color
        ctx.font = MONO_BOLD
        ctx.fillText(s.unit ? `${s.label} (${s.unit})` : s.label, pad.l + 4, top + 8)
      })

      // shared x axis: iteration number, the ledger's own key
      ctx.font = MONO_SM
      ctx.fillStyle = I.label
      ctx.textBaseline = "alphabetic"
      ctx.textAlign = "center"
      const first = runs[0]?.iteration ?? 0
      const last = runs[runs.length - 1]?.iteration ?? 0
      ctx.fillText(`#${first}`, pad.l, h - 4)
      ctx.fillText(`#${last}`, pad.l + plotW, h - 4)
      ctx.textAlign = "right"
      ctx.fillText(`iteration · ${runs.length} trials`, w - pad.r, h - 4)
    },
    [rows, targetBer],
  )
  return <canvas ref={ref} className="block h-full w-full" />
}
