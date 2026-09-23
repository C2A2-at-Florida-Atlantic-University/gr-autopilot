import type { ReactNode } from "react"

/** Every page names the endpoint it is the GUI for, as a link: the JSON is one click away and the
 *  page never pretends to be more than a rendering of it. */
export function PageHeader({ title, endpoint, hint, at, error, children }: {
  title: string
  endpoint: string
  hint?: string
  at?: number | null
  error?: string | null
  children?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-end gap-x-4 gap-y-2 border-b pb-2.5">
      <div>
        <h1 className="font-mono text-[16px] font-semibold">{title}</h1>
        {hint && <div className="text-[12px] text-muted-foreground">{hint}</div>}
      </div>
      <a
        href={"." + endpoint}
        target="_blank"
        rel="noreferrer"
        className="rounded border px-2 py-0.5 font-mono text-[11px] text-muted-foreground hover:border-primary/40 hover:text-primary"
      >
        GET {endpoint}
      </a>
      <div className="ml-auto flex items-center gap-3 font-mono text-[11px] text-muted-foreground">
        {children}
        {error ? <span className="text-bad">{error}</span>
               : at ? <span>fetched {new Date(at).toLocaleTimeString()}</span> : null}
      </div>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border bg-card p-6 text-center text-sm text-muted-foreground">{children}</div>
  )
}

export function Raw({ data }: { data: unknown }) {
  return (
    <details className="text-[11px] text-muted-foreground">
      <summary className="cursor-pointer select-none font-mono hover:text-foreground">raw JSON</summary>
      <pre className="mt-2 max-h-[60vh] overflow-auto rounded-lg border bg-muted/60 p-3 font-mono text-[11px] leading-relaxed text-foreground">
        {JSON.stringify(data, null, 2)}
      </pre>
    </details>
  )
}
