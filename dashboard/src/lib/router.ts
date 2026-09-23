import { useEffect, useState } from "react"

/**
 * Hash routing, one page per daemon endpoint, with an optional second segment (`#/docs/library`).
 * A hash rather than a path so the stdlib server's static catch-all (every unknown path serves
 * index.html) is never relied on for deep links, and a reload lands on the same page.
 */
export type PageId =
  | "overview" | "devices" | "topology" | "experiments" | "flowgraphs" | "prompts" | "tools"
  | "operator" | "docs"

export interface PageDef {
  id: PageId
  label: string
  endpoint: string // the daemon endpoint this page is the GUI for
  hint: string
}

export const PAGES: PageDef[] = [
  { id: "overview", label: "Overview", endpoint: "/data", hint: "the live link: health, plots, ledger" },
  { id: "devices", label: "Devices", endpoint: "/devices", hint: "radios, worker, path check" },
  { id: "topology", label: "Topology", endpoint: "/topology", hint: "the declared bench, drawn" },
  { id: "experiments", label: "Experiments", endpoint: "/experiments", hint: "what has been run" },
  { id: "flowgraphs", label: "Flowgraphs", endpoint: "/flowgraphs", hint: "exported .grc files" },
  { id: "prompts", label: "Prompts", endpoint: "/prompts", hint: "the experiment library" },
  { id: "tools", label: "Tools", endpoint: "/mcp", hint: "the agent's tool surface" },
  { id: "operator", label: "Operator", endpoint: "/control", hint: "channel, interferer, scenario" },
  { id: "docs", label: "Docs", endpoint: "/wiki", hint: "the documentation" },
]

export interface Route { page: PageId; sub: string | null }

function read(): Route {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/")
  const page = (PAGES.some((p) => p.id === parts[0]) ? parts[0] : "overview") as PageId
  const sub = parts[1] ? decodeURIComponent(parts[1]) : null
  return { page, sub }
}

export function useRoute(): Route & { navigate: (id: PageId, sub?: string) => void } {
  const [route, setRoute] = useState<Route>(read)
  useEffect(() => {
    const on = () => setRoute(read())
    window.addEventListener("hashchange", on)
    return () => window.removeEventListener("hashchange", on)
  }, [])
  return {
    ...route,
    navigate: (id, sub) => { window.location.hash = "/" + id + (sub ? "/" + encodeURIComponent(sub) : "") },
  }
}
