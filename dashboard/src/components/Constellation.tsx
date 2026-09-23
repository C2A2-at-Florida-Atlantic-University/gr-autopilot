import { useRef } from "react"
import { useCanvasDraw } from "@/lib/canvas"
import { alpha, ink, MONO_SM } from "@/lib/ink"

/** Recovered-symbol constellation with a DENSITY underlay (design review #2): overplotted scatter
 * hides how tight the clusters are; shading a coarse bin grid by sample count makes clusters vs a
 * smeared ring vs a fuzzy cloud read at a glance — the same read the diagnoser makes. I/Q axes
 * labelled (#7). Mirrors perception/render.py's PNG, now live. */
export function Constellation({ points }: { points: number[][] }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useCanvasDraw(
    ref,
    (ctx, w, h) => {
      const I = ink()
      ctx.clearRect(0, 0, w, h)
      const cx = w / 2
      const cy = h / 2
      const s = (Math.min(w, h) / 2) * 0.84

      // grid
      ctx.strokeStyle = I.grid
      ctx.lineWidth = 1
      for (const g of [-1, -0.5, 0, 0.5, 1]) {
        ctx.beginPath()
        ctx.moveTo(cx + g * s, 8); ctx.lineTo(cx + g * s, h - 8)
        ctx.moveTo(8, cy - g * s); ctx.lineTo(w - 8, cy - g * s)
        ctx.stroke()
      }

      if (points.length) {
        // density: accumulate into a coarse grid, shade by count (a cheap hexbin-alike)
        const gs = 9
        const cols = Math.ceil(w / gs)
        const rows = Math.ceil(h / gs)
        const bin = new Float32Array(cols * rows)
        let mx = 0
        for (const p of points) {
          const x = cx + p[0] * s
          const y = cy - p[1] * s
          const c = (x / gs) | 0
          const r = (y / gs) | 0
          if (c < 0 || r < 0 || c >= cols || r >= rows) continue
          const k = r * cols + c
          bin[k]++
          if (bin[k] > mx) mx = bin[k]
        }
        for (let r = 0; r < rows; r++) {
          for (let c = 0; c < cols; c++) {
            const v = bin[r * cols + c]
            if (!v) continue
            ctx.fillStyle = alpha(I.accent, Math.min(0.85, 0.1 + 0.9 * (v / mx)))
            ctx.fillRect(c * gs, r * gs, gs - 1, gs - 1)
          }
        }
        // faint scatter on top keeps individual samples visible
        ctx.fillStyle = alpha(I.ink, 0.32)
        for (const p of points) {
          ctx.beginPath()
          ctx.arc(cx + p[0] * s, cy - p[1] * s, 0.9, 0, Math.PI * 2)
          ctx.fill()
        }
      }

      // I/Q labels
      ctx.fillStyle = I.dim
      ctx.font = MONO_SM
      ctx.textAlign = "left"
      ctx.fillText("I", w - 13, cy - 5)
      ctx.fillText("Q", cx + 5, 12)
    },
    [points],
  )
  return <canvas ref={ref} className="block h-full w-full" />
}
