import { useRef, type ReactNode } from "react"
import { Glyph } from "@/components/Glyph"
import { canvasIn, downloadCanvasPng, figureFilename } from "@/lib/figures"

/**
 * A plate in the report: number, title, the plot, and a caption that says what to read from it.
 *
 * Every figure carries its own save control. A figure is the part of a run someone puts in a
 * slide or a note, and a report whose pictures can only be screenshotted is a report whose
 * pictures leave at the wrong resolution with the wrong background. The button exports what is
 * on screen, compositing the page background and stamping the caption underneath, so the file
 * still explains itself once it is somewhere else.
 */
export function Figure({
  n, title, caption, runSlug, height = 216, children, span,
}: {
  n: number
  title: string
  caption: ReactNode
  runSlug: string
  height?: number
  children: ReactNode
  span?: boolean
}) {
  const body = useRef<HTMLDivElement>(null)

  const save = () => {
    const c = canvasIn(body.current)
    if (!c) return
    downloadCanvasPng(
      c,
      figureFilename(runSlug, `fig${n}-${title}`),
      `Figure ${n}. ${title}`,
      runSlug,
    )
  }

  return (
    <figure
      data-fig-n={n}
      data-fig-title={title}
      className={`group/fig flex min-w-0 flex-col overflow-hidden rounded-xl border border-border-strong
                  bg-gradient-to-b from-card to-muted
                  shadow-[0_16px_40px_-30px_var(--shadow)] ${span ? "sm:col-span-2" : ""}`}
    >
      <figcaption className="flex items-center gap-2 border-b border-border/70 px-3 py-1.5">
        <span className="font-mono text-[10px] font-semibold uppercase tracking-[1.4px] text-primary">
          Fig {n}
        </span>
        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-foreground">{title}</span>
        <button
          onClick={save}
          title={`Download figure ${n} as PNG`}
          aria-label={`Download figure ${n} as PNG`}
          className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-border
                     bg-muted/60 text-muted-foreground transition-colors
                     hover:border-primary/40 hover:bg-primary/10 hover:text-primary
                     focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
        >
          <Glyph name="download" size={12} />
        </button>
      </figcaption>
      <div ref={body} style={{ height }} className="min-w-0 px-2 py-2">
        {children}
      </div>
      <div className="border-t border-border/60 px-3 py-2 text-[11.5px] leading-snug text-muted-foreground">
        {caption}
      </div>
    </figure>
  )
}
