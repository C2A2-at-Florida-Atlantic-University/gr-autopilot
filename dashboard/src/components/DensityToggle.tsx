import { useEffect, useState } from "react"
import { Glyph } from "@/components/Glyph"
import { resetInk } from "@/lib/ink"
import { cn } from "@/lib/utils"

/**
 * Comfortable / Compact. Density changes spacing and type step only — never which information is
 * present, and never the data density of the plots. A wall display or a long soak wants compact;
 * reading a result wants comfortable.
 *
 * Shaped exactly like ThemeToggle and sat beside it: both are display preferences rather than
 * anything about the experiment, so they share one control language and one corner of the page
 * instead of spending a word of the header on themselves.
 */
const KEY = "gra.density"
type Density = "comfortable" | "compact"

const OPTIONS: { density: Density; label: string }[] = [
  { density: "comfortable", label: "Comfortable spacing" },
  { density: "compact", label: "Compact spacing" },
]

export function DensityToggle() {
  const [density, setDensity] = useState<Density>(() => {
    try { return localStorage.getItem(KEY) === "compact" ? "compact" : "comfortable" } catch { return "comfortable" }
  })
  useEffect(() => {
    document.documentElement.dataset.density = density
    resetInk()   // tokens may have moved; the canvas cache must not outlive them
    try { localStorage.setItem(KEY, density) } catch { /* non-fatal */ }
  }, [density])
  return (
    <div role="group" aria-label="Display density"
         className="inline-flex shrink-0 items-center gap-0.5 rounded-full border bg-muted/40 p-0.5">
      {OPTIONS.map(({ density: d, label }) => {
        const on = density === d
        return (
          <button
            key={d}
            type="button"
            aria-pressed={on}
            aria-label={label}
            title={label}
            onClick={() => setDensity(d)}
            className={cn(
              "inline-flex h-[22px] w-[26px] items-center justify-center rounded-full transition-colors",
              "outline-none focus-visible:ring-2 focus-visible:ring-primary/60",
              on ? "bg-card text-foreground ring-1 ring-border" : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Glyph name={d} size={13} />
          </button>
        )
      })}
    </div>
  )
}
