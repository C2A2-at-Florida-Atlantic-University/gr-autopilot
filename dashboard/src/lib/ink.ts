/**
 * Plot ink, read from the CSS custom properties rather than hard-coded per renderer.
 *
 * Canvas renderers cannot use CSS classes, so without this each would carry its own colour
 * literals and sit permanently outside the token system: changing a token would move the chrome
 * and leave the charts behind, and no density or contrast setting could reach them. This reads
 * the same variables Tailwind is themed from, so there is one source.
 *
 * Cached because getComputedStyle is not free and these are read on every animation frame.
 * `resetInk()` drops the cache when the theme or density changes, and tells every canvas to
 * repaint: a plot otherwise only redraws on new data, and an idle console would keep the old ink.
 */
export interface Ink {
  grid: string
  label: string
  ink: string
  dim: string
  accent: string
  good: string
  warn: string
  bad: string
  /** Translucent backing for text drawn ON a plot, so a label over a trace stays readable. */
  plate: string
  trace: string[]
}

let cache: Ink | null = null

function read(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

export function ink(): Ink {
  if (cache) return cache
  cache = {
    grid: read("--grid", "#16222f"),
    label: read("--ink-faint", "#4a5b6c"),
    ink: read("--foreground", "#dbe4ee"),
    dim: read("--muted-foreground", "#7d8b9a"),
    accent: read("--primary", "#38bdf8"),
    good: read("--good", "#34d399"),
    warn: read("--warn", "#f5b13d"),
    bad: read("--bad", "#fb5a4b"),
    // Deliberately the PAGE behind the ink rather than any panel token: the job is to hide
    // whatever the label happens to land on, which is usually the trace, not the card.
    plate: isLight() ? "rgba(255, 255, 255, 0.86)" : "rgba(8, 11, 16, 0.74)",
    trace: [
      read("--trace-1", "#4ade80"),
      read("--trace-2", "#38bdf8"),
      read("--trace-3", "#f5b13d"),
      read("--trace-4", "#c084fc"),
      read("--trace-5", "#f472b6"),
    ],
  }
  return cache
}

/** Fired on window by resetInk(); useCanvasDraw repaints on it. */
export const INK_CHANGED = "gra:ink"

export function resetInk() {
  cache = null
  if (typeof window !== "undefined") window.dispatchEvent(new Event(INK_CHANGED))
}

/** True in the light theme — for the few renderers whose colour logic is not a single token. */
export function isLight(): boolean {
  return typeof document !== "undefined" && document.documentElement.dataset.theme === "light"
}

/** rgba() from a token hex, for fills that need transparency. */
export function alpha(hex: string, a: number): string {
  const h = hex.replace("#", "")
  const n = h.length === 3 ? h.split("").map((c) => c + c).join("") : h
  const int = parseInt(n, 16)
  return `rgba(${(int >> 16) & 255}, ${(int >> 8) & 255}, ${int & 255}, ${a})`
}

/** The plot type face, so canvas text matches the page. */
export const MONO = '11px "IBM Plex Mono", ui-monospace, monospace'
export const MONO_SM = '9.5px "IBM Plex Mono", ui-monospace, monospace'
export const MONO_BOLD = '600 9.5px "IBM Plex Mono", ui-monospace, monospace'
