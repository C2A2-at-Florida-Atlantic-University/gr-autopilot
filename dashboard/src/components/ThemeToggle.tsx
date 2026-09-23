import { useEffect, useState } from "react"
import { Glyph } from "@/components/Glyph"
import { resetInk } from "@/lib/ink"
import { cn } from "@/lib/utils"

/**
 * Light / Dark. Dark stays the default — it is the instrument-screen look the console was designed
 * around; light is for a bright room or a projector. The choice is remembered per browser, and
 * index.html applies it before first paint so a reload never flashes the other theme.
 */
const KEY = "gra.theme"
type Theme = "light" | "dark"

const OPTIONS: { theme: Theme; label: string }[] = [
  { theme: "light", label: "Light theme" },
  { theme: "dark", label: "Dark theme" },
]

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => {
    try { return localStorage.getItem(KEY) === "light" ? "light" : "dark" } catch { return "dark" }
  })
  useEffect(() => {
    const root = document.documentElement
    root.dataset.theme = theme
    // The page colour outside <body> (overscroll) and the browser chrome follow the theme too.
    const bg = getComputedStyle(root).getPropertyValue("--background").trim()
    root.style.background = bg
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", bg)
    resetInk()   // tokens moved; the canvases repaint from the new ink
    try { localStorage.setItem(KEY, theme) } catch { /* non-fatal */ }
  }, [theme])
  return (
    <div role="group" aria-label="Colour theme"
         className="inline-flex shrink-0 items-center gap-0.5 rounded-full border bg-muted/40 p-0.5">
      {OPTIONS.map(({ theme: t, label }) => {
        const on = theme === t
        return (
          <button
            key={t}
            type="button"
            aria-pressed={on}
            aria-label={label}
            title={label}
            onClick={() => setTheme(t)}
            className={cn(
              "inline-flex h-[22px] w-[26px] items-center justify-center rounded-full transition-colors",
              "outline-none focus-visible:ring-2 focus-visible:ring-primary/60",
              on ? "bg-card text-foreground ring-1 ring-border" : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Glyph name={t === "light" ? "sun" : "moon"} size={13} />
          </button>
        )
      })}
    </div>
  )
}
