import { useState } from "react"
import { useJson } from "@/lib/useJson"
import { PAGES } from "@/lib/router"
import { PageHeader, Empty, Raw } from "@/components/PageHeader"
import { Card } from "@/components/ui/card"
import type { Prompt, PromptsPayload } from "@/types"

const P = PAGES.find((p) => p.id === "prompts")!

/**
 * How the library is presented: the paper's experiments, in the order the paper reports them,
 * each opened with its prompt showing. A topic the daemon serves that is not named here still
 * renders — the library can grow without editing this file — it just sorts to the end.
 */
const TOPICS: { id: string; label: string; blurb: string }[] = [
  { id: "paper",
    label: "The paper's experiments",
    blurb: "The three runs reported in the paper, word for word. Each is a whole session: a goal, a method that forces the agent to measure rather than assume, and the shape of the report it has to come back with." },
]

function Copy({ text }: { text: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      onClick={() => { navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1400) }).catch(() => undefined) }}
      className={`shrink-0 rounded border px-2 py-0.5 font-mono text-[10.5px] transition-colors ${
        done ? "border-good/50 text-good" : "border-border text-muted-foreground hover:border-primary/40 hover:text-primary"
      }`}
    >
      {done ? "copied" : "copy"}
    </button>
  )
}

function PromptCard({ p, index }: { p: Prompt; index?: number }) {
  // The paper's three open with the prompt showing. They are the reason most people arrive on
  // this page, and making the headline exercise a click away from being readable is a poor trade.
  const [open, setOpen] = useState(index !== undefined)
  return (
    <Card tier={index !== undefined ? "secondary" : "tertiary"} className="px-3.5 py-2.5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {index !== undefined && (
          <span className="rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[10.5px] font-semibold text-primary">
            {index}
          </span>
        )}
        <button
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="text-left text-[13px] font-semibold hover:text-primary"
        >
          <span className="mr-1.5 inline-block w-[9px] font-mono text-muted-foreground">{open ? "▾" : "▸"}</span>
          {p.title}
        </button>
        <span className="font-mono text-[10.5px] text-muted-foreground">{p.name}</span>
        <span className="ml-auto flex items-center gap-2"><Copy text={p.body} /></span>
      </div>
      <p className="mt-0.5 max-w-[80ch] text-[12px] text-muted-foreground">{p.description}</p>
      {p.arguments.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-[11px] text-muted-foreground">
          {p.arguments.map((a) => (
            <span key={a.name} title={a.description}>
              <span className="text-foreground">{a.name}</span>{a.required ? "*" : ""}{a.default !== undefined ? ` = ${a.default}` : ""}
            </span>
          ))}
        </div>
      )}
      {p.notes && (
        <p className="mt-1.5 max-w-[90ch] border-l-2 border-warn/60 pl-2 text-[11.5px] text-muted-foreground">
          <span className="font-semibold text-foreground">On the bench: </span>{p.notes}
        </p>
      )}
      {open && <pre className="mt-2 max-h-[50vh] overflow-auto whitespace-pre-wrap rounded-lg border bg-muted/60 p-3 font-mono text-[11.5px] leading-relaxed">{p.body}</pre>}
    </Card>
  )
}

/** The experiment library, as the MCP prompts/list would hand it to a client — readable here
 *  before one is connected. */
export function PromptsPage() {
  const q = useJson<PromptsPayload>("./prompts")
  const prompts = q.data?.prompts ?? []
  const known = TOPICS.map((t) => t.id)
  const extra = Array.from(new Set(prompts.map((p) => p.topic))).filter((t) => !known.includes(t))
  const sections = [...TOPICS, ...extra.map((id) => ({ id, label: id, blurb: "" }))]

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Prompts"
        endpoint={P.endpoint}
        hint="the experiment library — paste one into a connected LLM, or open it as an MCP prompt"
        at={q.at}
        error={q.error}
      />
      {prompts.length === 0 ? (
        <Empty>{q.loading ? "loading…" : "No prompts in the library."}</Empty>
      ) : (
        sections.map((t) => {
          const rows = prompts.filter((p) => p.topic === t.id)
          if (!rows.length) return null
          const paper = t.id === "paper"
          return (
            <section key={t.id} className="flex flex-col gap-2">
              <div className={paper ? "rounded-lg border border-primary/30 bg-primary/5 px-3 py-2" : ""}>
                <h2 className={`text-[10px] uppercase tracking-[2px] ${paper ? "text-primary" : "text-muted-foreground"}`}>
                  {t.label}
                </h2>
                {t.blurb && <p className="mt-0.5 max-w-[95ch] text-[12px] text-muted-foreground">{t.blurb}</p>}
              </div>
              {rows.map((p, i) => (
                <PromptCard key={p.name} p={p} index={paper ? i + 1 : undefined} />
              ))}
            </section>
          )
        })
      )}
      {q.data && <Raw data={q.data} />}
    </div>
  )
}
