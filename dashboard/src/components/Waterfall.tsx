import { useRef } from "react"
import { useCanvasDraw } from "@/lib/canvas"
import { alpha, ink, isLight, MONO, MONO_SM } from "@/lib/ink"
import { fmtCentreCompact, fmtOffset, freqAxis, heat, placeLabels } from "@/lib/axis"
import type { Spectrum as Spec } from "@/types"

/**
 * Spectrum history as a waterfall.
 *
 * A single PSD trace shows the band as it is in one instant, which is the wrong instrument for
 * the question this bench actually asks — did something appear, and when? A jammer switching on,
 * a hop, a neighbour's burst: all of those are events in TIME, invisible in a trace that is
 * redrawn from scratch each frame. Stacking the traces makes the time axis explicit.
 *
 * The history is accumulated client-side, one row per snapshot, keyed on the frame counter `t`,
 * because the server's snapshot is a last-value store with no memory of its own. That means the
 * waterfall starts empty on a reload and fills as runs complete — it is a live record, not a
 * reconstruction, and it never shows a row that did not come from a completed trial.
 */
const MAX_ROWS = 96

export function Waterfall({ spectrum, t, experimentKey = "", freqHz = null }: {
  spectrum: Spec
  t: number
  experimentKey?: string
  /** Centre frequency this row was captured on, so a retune can be ruled across the history. */
  freqHz?: number | null
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const rows = useRef<{ t: number; psd: number[]; freqHz: number | null }[]>([])
  const lastT = useRef<number>(-1)
  const lastKey = useRef<string>(experimentKey)

  // A different experiment is a different history. Its frame counter restarts at 1, so without
  // this the old rows would sit under the new ones and read as one continuous record.
  if (experimentKey !== lastKey.current) {
    lastKey.current = experimentKey
    rows.current = []
    lastT.current = -1
  }

  // Append only on a NEW frame. The page polls faster than trials complete, so without this the
  // waterfall would fill with duplicates of the same measurement and imply activity that is not
  // happening.
  if (spectrum?.psd_db?.length && t !== lastT.current) {
    lastT.current = t
    rows.current = [...rows.current, { t, psd: spectrum.psd_db, freqHz }].slice(-MAX_ROWS)
  }

  useCanvasDraw(
    ref,
    (ctx, w, h) => {
      const I = ink()
      const light = isLight()
      ctx.clearRect(0, 0, w, h)
      const pad = { t: 6, b: 18, l: 34, r: 8 }
      const plotW = w - pad.l - pad.r
      const plotH = h - pad.t - pad.b
      const hist = rows.current
      const nBins = spectrum?.psd_db?.length ?? 0

      if (!hist.length || !nBins || plotW < 4 || plotH < 4) {
        ctx.fillStyle = I.label
        ctx.font = MONO
        ctx.textAlign = "center"
        ctx.fillText("waiting for the next trial…", w / 2, h / 2)
        return
      }

      // One image row per trial, newest at the TOP so the eye lands on the present first.
      const img = ctx.createImageData(nBins, hist.length)
      const floor = -60
      for (let r = 0; r < hist.length; r++) {
        const psd = hist[hist.length - 1 - r].psd
        for (let c = 0; c < nBins; c++) {
          const db = Math.max(psd[c] ?? floor, floor)
          const [R, G, B] = heat((db - floor) / -floor, light)
          const o = (r * nBins + c) * 4
          img.data[o] = R; img.data[o + 1] = G; img.data[o + 2] = B; img.data[o + 3] = 255
        }
      }
      // Blit through an offscreen canvas so the browser scales it to the panel for us.
      const off = document.createElement("canvas")
      off.width = nBins; off.height = hist.length
      off.getContext("2d")!.putImageData(img, 0, 0)
      ctx.imageSmoothingEnabled = false
      ctx.drawImage(off, pad.l, pad.t, plotW, plotH)

      // ---- frequency axis, absolute where the snapshot says what it is ----
      ctx.font = MONO_SM
      ctx.fillStyle = I.label
      ctx.textAlign = "center"
      ctx.textBaseline = "alphabetic"
      const span = spectrum.span_hz
      const centre = spectrum.center_hz
      if (span && centre) {
        // Same axis as the PSD above it, so the two panels line up and read as one instrument.
        const axis = freqAxis(centre, span, 4)
        const lo = centre - span / 2
        const reserved: [number, number][] = [[0, pad.l - 6], [w - pad.r + 2, w]]

        const xOf = (hz: number) => pad.l + ((hz - lo) / span) * plotW
        const shown = placeLabels(
          axis.ticks.filter((t) => t.hz >= lo && t.hz <= lo + span),
          (t) => xOf(t.hz),
          (t) => ctx.measureText(t.label).width / 2,
          (t) => (t.centre ? 2 : 1),
          10,
          reserved,
        )
        // Gridlines follow every tick the axis produced, not just the ones that kept a label —
        // thinning text must not thin the geometry the reader is measuring against.
        ctx.strokeStyle = light ? alpha(I.ink, 0.18) : "rgba(255,255,255,0.16)"
        for (const t of axis.ticks) {
          const x = xOf(t.hz)
          if (x < pad.l - 1 || x > w - pad.r + 1) continue
          ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + plotH); ctx.stroke()
        }
        for (const t of shown) ctx.fillText(t.label, xOf(t.hz), h - 6)
      } else {
        for (const t2 of [-0.5, -0.25, 0, 0.25, 0.5]) {
          const x = pad.l + (t2 + 0.5) * plotW
          ctx.fillText(t2.toFixed(2), x, h - 6)
        }
        ctx.textAlign = "right"
        ctx.fillText("f/Rs", w - pad.r, h - 6)
      }

      // ---- time axis: newest at top, labelled by how many trials back ----
      ctx.textAlign = "right"
      ctx.textBaseline = "middle"
      const rowH = plotH / hist.length
      for (const back of [0, Math.floor(hist.length / 2), hist.length - 1]) {
        if (back >= hist.length) continue
        const y = pad.t + (back + 0.5) * rowH
        const label = back === 0 ? "now" : `-${back}`
        ctx.fillStyle = back === 0 ? I.dim : I.label
        ctx.fillText(label, pad.l - 4, y)
      }

      // ---- retune rules: where the link changed channel -------------------------------------
      //
      // Not decoration. The frequency axis is drawn from the CURRENT centre, so rows captured
      // before a retune are plotted against a reference they were not measured on — a hop of
      // 10 MHz moves the whole band under the picture and the trace above and below the rule are
      // not the same 500 kHz of spectrum. The rule is where that reference changed, which makes
      // the discontinuity in the image readable instead of mysterious, and it is the clearest
      // statement the console has that frequency avoidance actually happened.
      ctx.textBaseline = "alphabetic"
      for (let j = 1; j < hist.length; j++) {
        const prev = hist[j - 1].freqHz
        const now = hist[j].freqHz
        if (prev == null || now == null || Math.abs(prev - now) < 1e3) continue
        const y = pad.t + (hist.length - 1 - j + 1) * rowH   // top edge of the first row after it
        ctx.strokeStyle = I.accent
        ctx.lineWidth = 1.5
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke()
        ctx.lineWidth = 1

        // The label sits above its rule, or below it when the rule is near the top edge — the
        // newest rows are at the top, so the most recent retune is exactly the one that would
        // otherwise write itself off the plot.
        const label = `↱ ${fmtCentreCompact(now)}`
        ctx.font = MONO_SM
        const tw = ctx.measureText(label).width
        const below = y - 11 < pad.t + 11        // clear of the "N trials" caption too
        const boxY = below ? y + 1 : y - 11
        ctx.fillStyle = I.plate
        ctx.fillRect(pad.l + 2, boxY, tw + 6, 12)
        ctx.fillStyle = I.accent
        ctx.textAlign = "left"
        ctx.fillText(label, pad.l + 5, boxY + 10)
      }

      // ---- caption: what the axes MEAN, since a waterfall invites over-reading ----
      ctx.textAlign = "left"
      ctx.textBaseline = "alphabetic"
      ctx.fillStyle = I.label
      ctx.font = MONO_SM
      ctx.fillText(`${hist.length} trials`, pad.l + 2, pad.t + 9)
      if (span) {
        // The centre the offset ticks are measured from is on the axis itself, at the centre
        // tick; this corner carries only what the axis does not say.
        ctx.textAlign = "right"
        ctx.fillStyle = I.label
        ctx.fillText(`span ${fmtOffset(span).replace("+", "")} (symbol rate)`, w - pad.r, pad.t + 9)
      }
    },
    [spectrum, t],
  )
  return <canvas ref={ref} className="block h-full w-full" />
}
