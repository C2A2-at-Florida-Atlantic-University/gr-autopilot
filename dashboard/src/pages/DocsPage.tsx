import { type MouseEvent } from "react"
import { useJson } from "@/lib/useJson"
import { PAGES, type PageId } from "@/lib/router"
import { PageHeader, Empty } from "@/components/PageHeader"
import type { WikiFragment } from "@/types"

const P = PAGES.find((p) => p.id === "docs")!
const APP_PAGES = new Set(PAGES.map((p) => p.id))

/**
 * The documentation, inside the dashboard's shell. The daemon renders each page from its Markdown
 * source and hands the body over as data; this page places it under the same header and
 * navigation as everything else, so reading the docs is not a change of layout.
 *
 * Links inside the rendered text are kept in the shell: a link to another wiki page becomes a
 * route, a link to an endpoint becomes its page, and a same-page anchor scrolls without
 * disturbing the route in the hash.
 */
export function DocsPage({ sub, navigate }: { sub: string | null; navigate: (id: PageId, sub?: string) => void }) {
  const name = sub || "index"
  const q = useJson<WikiFragment>(`./wiki/${encodeURIComponent(name)}?fragment=1`)

  const onClick = (e: MouseEvent<HTMLElement>) => {
    const a = (e.target as HTMLElement).closest("a")
    if (!a) return
    const href = a.getAttribute("href") ?? ""
    const wiki = href.match(/^\/wiki\/?([a-z-]*)$/)
    if (wiki) { e.preventDefault(); navigate("docs", wiki[1] || "index"); return }
    const ep = href.match(/^\/([a-z]+)\/?$/)
    if (ep && APP_PAGES.has(ep[1] as PageId)) { e.preventDefault(); navigate(ep[1] as PageId); return }
    if (href.startsWith("#") && !href.startsWith("#/")) {
      e.preventDefault()
      const smooth = !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
      document.getElementById(decodeURIComponent(href.slice(1)))
        ?.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" })
    }
  }

  const d = q.data
  return (
    <div className="flex flex-col gap-3">
      <PageHeader title={d?.title ?? "Documentation"} endpoint={`/wiki/${name}?fragment=1`}
                  hint={d ? `${P.hint} · ${d.name}` : P.hint} at={q.at} error={q.error} />
      {q.error && !d ? (
        <Empty><span className="text-bad">{q.error}</span>{q.error.includes("404") ? ` — no page named "${name}".` : ""}</Empty>
      ) : !d ? <Empty>loading…</Empty> : (
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
          <article className="prose min-w-0 flex-1" onClick={onClick} dangerouslySetInnerHTML={{ __html: d.body }} />
          {d.toc && (
            <aside className="toc shrink-0 rounded-lg border bg-muted/40 px-3 py-2.5 lg:sticky lg:top-4 lg:w-[220px]" onClick={onClick}>
              <div className="mb-1 text-[9.5px] uppercase tracking-[2px] text-muted-foreground">on this page</div>
              <div dangerouslySetInnerHTML={{ __html: d.toc }} />
            </aside>
          )}
        </div>
      )}
      {d && (d.prev || d.next) && (
        <div className="flex justify-between gap-3 border-t pt-3 text-[12px]">
          {d.prev ? <a href={`#/docs/${d.prev.name}`} className="text-primary hover:underline">← {d.prev.title}</a> : <span />}
          {d.next ? <a href={`#/docs/${d.next.name}`} className="text-primary hover:underline">{d.next.title} →</a> : <span />}
        </div>
      )}
    </div>
  )
}
