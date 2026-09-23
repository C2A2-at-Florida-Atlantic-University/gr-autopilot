import { ink } from "@/lib/ink"

/**
 * Saving what is on screen.
 *
 * The plots are canvases drawn with `clearRect`, so their background is transparent. Exported as
 * they are, a figure with pale axis labels lands in a document as pale labels on nothing — legible
 * in the dark console, invisible on white paper. So an export composites the page background in
 * first, at twice the display resolution, and stamps a caption underneath: a figure that leaves
 * the console stops having the console around it to explain what it is.
 */

function ts(): string {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "").replace("T", "-")
}

/** A filesystem-safe stem: lowercase, words joined by a single dash. */
export function slugify(s: string): string {
  return (s || "figure").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "figure"
}

function triggerDownload(href: string, filename: string, revoke = false) {
  const a = document.createElement("a")
  a.href = href
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  if (revoke) setTimeout(() => URL.revokeObjectURL(href), 4000)
}

export function downloadText(filename: string, text: string, mime = "text/plain;charset=utf-8") {
  triggerDownload(URL.createObjectURL(new Blob([text], { type: mime })), filename, true)
}

/**
 * Render a canvas to a PNG file with an opaque background and a caption strip.
 *
 * `scale` is applied on top of the canvas's own device-pixel backing store, so a figure exported
 * from a HiDPI screen is already sharp and this only makes it sharper.
 */
export function downloadCanvasPng(
  source: HTMLCanvasElement,
  filename: string,
  caption?: string,
  subcaption?: string,
) {
  const I = ink()
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--background").trim() || "#080b10"
  const scale = 2
  const w = source.width * scale
  const capH = caption ? Math.round(46 * scale) : 0
  const pad = Math.round(14 * scale)

  const out = document.createElement("canvas")
  out.width = w + pad * 2
  out.height = source.height * scale + capH + pad * 2
  const ctx = out.getContext("2d")
  if (!ctx) return

  ctx.fillStyle = bg
  ctx.fillRect(0, 0, out.width, out.height)
  ctx.imageSmoothingEnabled = true
  ctx.imageSmoothingQuality = "high"
  ctx.drawImage(source, 0, 0, source.width, source.height, pad, pad, w, source.height * scale)

  if (caption) {
    const y = pad + source.height * scale + Math.round(22 * scale)
    ctx.textAlign = "left"
    ctx.fillStyle = I.ink
    ctx.font = `600 ${Math.round(12 * scale)}px "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif`
    ctx.fillText(caption, pad, y)
    if (subcaption) {
      ctx.fillStyle = I.dim
      ctx.font = `${Math.round(10.5 * scale)}px "IBM Plex Mono", ui-monospace, monospace`
      ctx.fillText(subcaption, pad, y + Math.round(15 * scale))
    }
  }

  out.toBlob((b) => {
    if (b) triggerDownload(URL.createObjectURL(b), filename, true)
  }, "image/png")
}

/** The canvas inside a figure frame. Figures are drawn by existing components, which own their
 *  own canvas element, so the frame finds it rather than threading a ref through each one. */
export function canvasIn(el: HTMLElement | null): HTMLCanvasElement | null {
  return el?.querySelector("canvas") ?? null
}

export function figureFilename(runSlug: string, figure: string, ext = "png"): string {
  return `${slugify(runSlug)}-${slugify(figure)}-${ts()}.${ext}`
}
