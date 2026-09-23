import { useEffect, type RefObject } from "react"
import { INK_CHANGED } from "@/lib/ink"

/** Draw to a canvas at device-pixel resolution, re-drawing on data change, on resize, and when the
 *  theme or density changes the ink. */
export function useCanvasDraw(
  ref: RefObject<HTMLCanvasElement | null>,
  draw: (ctx: CanvasRenderingContext2D, w: number, h: number) => void,
  deps: unknown[],
) {
  useEffect(() => {
    const c = ref.current
    if (!c) return
    const render = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const w = c.clientWidth
      const h = c.clientHeight
      c.width = Math.max(1, w * dpr)
      c.height = Math.max(1, h * dpr)
      const ctx = c.getContext("2d")
      if (!ctx) return
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      draw(ctx, w, h)
    }
    render()
    const ro = new ResizeObserver(render)
    ro.observe(c)
    window.addEventListener(INK_CHANGED, render)
    return () => {
      ro.disconnect()
      window.removeEventListener(INK_CHANGED, render)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
}
