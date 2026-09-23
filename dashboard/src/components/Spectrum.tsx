import { useRef } from "react"
import { useCanvasDraw } from "@/lib/canvas"
import { alpha, ink, MONO_BOLD, MONO_SM } from "@/lib/ink"
import { fmtOffset, freqAxis, placeLabels } from "@/lib/axis"
import type { InterfererEvidence } from "@/lib/link"
import type { Spectrum as Spec } from "@/types"

/** A piece of text the plot wants to draw, and where. Collected rather than drawn immediately so
 *  the annotations can be laid out against each other instead of written on top of one another. */
interface Note {
  x: number
  y: number
  text: string
  colour: string
  /** Higher wins the space when two would collide. */
  rank: number
}

/**
 * Received PSD with labelled axes, a peak cursor, and — only when something actually detected one
 * — an interferer flag.
 *
 * The component does not judge the flag from its own trace. This transform is fed the RECOVERED
 * SYMBOLS — matched-filtered, one sample per symbol — so the trace is the modulation's own
 * spectrum plus noise, and a threshold test on it (an off-centre bin above some level) fires on a
 * healthy link's shoulders as readily as on a jammer. An indicator that never changes teaches the
 * reader to ignore the one time it matters.
 *
 * The flag is therefore passed in as EVIDENCE or not at all. `evidence` comes from an energy-detection sweep
 * taken with the link silent (`sense_spectrum`), or failing that from the diagnoser's spur read,
 * and it carries the margin and the threshold it was judged against so the flag can show its
 * working rather than assert a conclusion.
 */
export function Spectrum({ spectrum, evidence }: {
  spectrum: Spec
  evidence?: InterfererEvidence | null
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  useCanvasDraw(
    ref,
    (ctx, w, h) => {
      const I = ink()
      ctx.clearRect(0, 0, w, h)
      const f = spectrum.freqs
      const p = spectrum.psd_db
      const floor = -80
      const pad = { t: 8, b: 18, l: 34, r: 8 }
      const plotW = w - pad.l - pad.r
      const plotH = h - pad.t - pad.b
      const Y = (db: number) => pad.t + (-Math.max(db, floor) / -floor) * plotH

      // dB gridlines + labels
      ctx.strokeStyle = I.grid
      ctx.lineWidth = 1
      ctx.fillStyle = I.label
      ctx.font = MONO_SM
      ctx.textAlign = "left"
      ctx.textBaseline = "alphabetic"
      for (let db = 0; db >= floor; db -= 20) {
        const y = Y(db)
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke()
        ctx.fillText(db === 0 ? "0 dB" : `${db}`, 2, y - 2)
      }

      if (!f.length) return
      const X = (i: number) => pad.l + (i / (f.length - 1)) * plotW

      // trace: fill + line
      ctx.beginPath()
      ctx.moveTo(X(0), h - pad.b)
      for (let i = 0; i < f.length; i++) ctx.lineTo(X(i), Y(p[i]))
      ctx.lineTo(X(f.length - 1), h - pad.b)
      ctx.closePath()
      ctx.fillStyle = alpha(I.trace[0], 0.14)
      ctx.fill()
      ctx.beginPath()
      ctx.moveTo(X(0), Y(p[0]))
      for (let i = 1; i < f.length; i++) ctx.lineTo(X(i), Y(p[i]))
      ctx.strokeStyle = I.trace[0]
      ctx.lineWidth = 1.7
      ctx.stroke()

      // ---- frequency axis --------------------------------------------------------------------
      // freqAxis decides absolute-vs-offset and the precision. The old code asked for three
      // decimals in whatever unit the magnitude chose, which printed "2.320 GHz" under every tick
      // of a 521 kHz span and "2 GHz" under the one that landed on a whole megahertz.
      ctx.font = MONO_SM
      ctx.fillStyle = I.label
      ctx.textAlign = "center"
      const span = spectrum.span_hz
      const centre = spectrum.center_hz
      const baseline = h - 5

      if (span && centre) {
        const axis = freqAxis(centre, span, 4)
        const lo = centre - span / 2

        // No in-plot caption: the panel title above already reads "centred 2320.000 MHz", and a
        // second copy of it in the bottom-right corner cost the +200 kHz tick its place. The
        // reserved regions here are just the plot's own edges, so a label never hangs off one.
        const reserved: [number, number][] = [[0, pad.l - 6], [w - pad.r + 2, w]]

        const shown = placeLabels(
          axis.ticks.filter((t) => t.hz >= lo && t.hz <= lo + span),
          (t) => pad.l + ((t.hz - lo) / span) * plotW,
          (t) => ctx.measureText(t.label).width / 2,
          (t) => (t.centre ? 2 : 1),
          10,
          reserved,
        )
        for (const t of shown) {
          ctx.fillText(t.label, pad.l + ((t.hz - lo) / span) * plotW, baseline)
        }

        // Centre marker: the carrier itself, so the eye has a fixed reference as the link retunes.
        const cx = pad.l + 0.5 * plotW
        ctx.strokeStyle = alpha(I.accent, 0.35)
        ctx.setLineDash([2, 3])
        ctx.beginPath(); ctx.moveTo(cx, pad.t); ctx.lineTo(cx, h - pad.b); ctx.stroke()
        ctx.setLineDash([])
      } else {
        for (const t of [-0.5, -0.25, 0, 0.25, 0.5]) {
          const i = (t + 0.5) * (f.length - 1)
          ctx.fillText(t.toFixed(2), X(i), baseline)
        }
        ctx.textAlign = "right"
        ctx.fillText("f/Rs", w - pad.r, baseline)
      }

      // ---- cursors ---------------------------------------------------------------------------
      // Both markers are drawn, but their LABELS are laid out together: the peak and the strongest
      // spur are often the same feature or a few pixels apart, and two strings written at the same
      // place are worse than one, so the weaker one steps aside rather than overprinting.
      const notes: Note[] = []

      let pk = 0
      for (let i = 1; i < p.length; i++) if (p[i] > p[pk]) pk = i
      const px = X(pk), py = Y(p[pk])
      ctx.strokeStyle = alpha(I.warn, 0.5)
      ctx.setLineDash([3, 3])
      ctx.beginPath(); ctx.moveTo(px, pad.t); ctx.lineTo(px, h - pad.b); ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = I.warn
      ctx.beginPath(); ctx.arc(px, py, 2.6, 0, Math.PI * 2); ctx.fill()
      notes.push({
        x: px, y: py - 6, colour: I.warn, rank: 2,
        text: span && centre
          ? `peak ${fmtOffset(f[pk] * span)}`     // offset from the carrier, which is what matters
          : `peak ${f[pk] >= 0 ? "+" : ""}${f[pk].toFixed(2)}`,
      })

      // Interferer flag. Drawn only when a measurement outside this plot says the channel is
      // occupied — never inferred from the shape of the trace, which cannot tell a jammer from
      // the modulation's own shoulders.
      if (evidence) {
        // A sweep measures a whole channel and cannot localise inside it, so its flag sits on the
        // carrier; a spur read knows its offset and is drawn where it was found.
        const frac = evidence.offsetHz != null && span
          ? Math.max(-0.5, Math.min(0.5, evidence.offsetHz / span))
          : 0
        const sx = pad.l + (frac + 0.5) * plotW
        const idx = Math.max(0, Math.min(f.length - 1, Math.round((frac + 0.5) * (f.length - 1))))
        const sy = Y(p[idx])

        ctx.strokeStyle = alpha(I.bad, 0.55)
        ctx.setLineDash([2, 4])
        ctx.beginPath(); ctx.moveTo(sx, pad.t); ctx.lineTo(sx, h - pad.b); ctx.stroke()
        ctx.setLineDash([])
        ctx.fillStyle = I.bad
        ctx.beginPath(); ctx.arc(sx, sy, 2.6, 0, Math.PI * 2); ctx.fill()

        // The number is the point: "+15.4 dB over noise, against a 6 dB threshold" is checkable,
        // "interferer" is not. The source is named because the two are not equally strong claims.
        const margin = `${evidence.marginDb >= 0 ? "+" : ""}${evidence.marginDb.toFixed(1)} dB`
        notes.push({
          x: sx, y: sy - 6, colour: I.bad, rank: 3,
          text: evidence.source === "sweep" ? `⚑ occupied ${margin}` : `⚑ spur ${margin}`,
        })
      }

      ctx.font = MONO_BOLD

      // A cursor label sits BESIDE its marker, never centred on it: centred, the dot is drawn
      // through the middle of the word. It takes the side with room, flipping near an edge, and
      // its box is measured on that side so two labels can be told apart before either is drawn.
      const side = (n: Note): "left" | "right" => {
        const wdt = ctx.measureText(n.text).width
        if (n.x + 6 + wdt <= w - pad.r) return "left"      // text to the RIGHT of the marker
        if (n.x - 6 - wdt >= pad.l) return "right"
        return n.x > (pad.l + w - pad.r) / 2 ? "right" : "left"
      }
      const textX = (n: Note) => (side(n) === "left" ? n.x + 6 : n.x - 6)
      const box = (n: Note): [number, number, number, number] => {
        const wdt = ctx.measureText(n.text).width
        const tx = textX(n)
        const [x0, x1] = side(n) === "left" ? [tx, tx + wdt] : [tx - wdt, tx]
        return [x0 - 3, n.y - 9, x1 + 3, n.y + 2]
      }
      const hits = (a: Note, b: Note) => {
        const [ax0, ay0, ax1, ay1] = box(a)
        const [bx0, by0, bx1, by1] = box(b)
        return ax0 < bx1 && ax1 > bx0 && ay0 < by1 && ay1 > by0
      }

      // Highest rank keeps its row; anything that would be written through it steps down, and is
      // dropped only when there is nowhere left on the plot to put it.
      const drawn: Note[] = []
      for (const n of [...notes].sort((a, b) => b.rank - a.rank)) {
        if (n.y < pad.t + 9) n.y += 22          // a label off the top lands under its marker
        let tries = 0
        while (drawn.some((d) => hits(d, n)) && tries < 4) { n.y += 13; tries++ }
        if (n.y > h - pad.b - 2 || drawn.some((d) => hits(d, n))) continue
        drawn.push(n)
      }
      // Each label gets a plate under it. A cursor lands wherever its feature is, which is
      // routinely on top of the trace, and coloured text over a bright green line is not
      // readable at 10px — the number is the whole reason the cursor is there.
      for (const n of drawn) {
        const [x0, y0, x1, y1] = box(n)
        ctx.fillStyle = I.plate
        ctx.fillRect(x0, y0, x1 - x0, y1 - y0)
        ctx.fillStyle = n.colour
        ctx.textAlign = side(n) === "left" ? "left" : "right"
        ctx.fillText(n.text, textX(n), n.y)
      }
      ctx.textAlign = "left"
    },
    [spectrum],
  )
  return <canvas ref={ref} className="block h-full w-full" />
}
