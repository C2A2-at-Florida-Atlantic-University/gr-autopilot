import { PAGES, type PageId } from "@/lib/router"
import type { Telemetry } from "@/lib/telemetry"
import { useJson } from "@/lib/useJson"
import { DensityToggle } from "@/components/DensityToggle"
import { ThemeToggle } from "@/components/ThemeToggle"
import type { WikiFragment } from "@/types"

/**
 * One entry per daemon endpoint, plus the standing state a reader should never have to hunt for:
 * whether the agent is live, whether the radios answer, the bench verdict, and above all whether
 * the interferer is keyed — a transmitter left on is the one state this console must never hide
 * behind a page the operator is not looking at.
 */
function Dot({ tone, pulse }: { tone: "good" | "bad" | "warn" | "muted"; pulse?: boolean }) {
  const cls = { good: "bg-good", bad: "bg-bad", warn: "bg-warn", muted: "bg-muted-foreground/50" }[tone]
  return <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${cls} ${pulse ? "animate-pulse" : ""}`} />
}

export function Nav({ page, sub, navigate, tele }: {
  page: PageId
  sub: string | null
  navigate: (id: PageId, sub?: string) => void
  tele: Telemetry
}) {
  // The wiki's own page list, shown beneath "Docs" while a documentation page is open.
  const wiki = useJson<WikiFragment>(page === "docs" ? "./wiki/index?fragment=1" : null)
  const devs = tele.devices?.devices ?? {}
  const roles = Object.entries(devs)
  const anyDown = roles.some(([, d]) => !d.alive)
  const jam = tele.devices?.jammer
  const topo = tele.topology
  const path = tele.devices?.path

  const items = PAGES.map((p) => {
    const active = p.id === page
    return (
      <div key={p.id} className="flex shrink-0 flex-col lg:w-full">
        <a
          href={"#/" + p.id}
          onClick={(e) => { e.preventDefault(); navigate(p.id) }}
          aria-current={active ? "page" : undefined}
          className={`group flex items-baseline gap-2 rounded-md border-l-2 px-3 py-1.5 text-[13px] transition-colors ${
            active ? "border-primary bg-primary/10 text-foreground" : "border-transparent text-muted-foreground hover:bg-muted hover:text-foreground"
          }`}
        >
          <span className="font-medium">{p.label}</span>
          <span className="hidden font-mono text-[10px] opacity-60 lg:inline">{p.endpoint}</span>
        </a>
        {p.id === "docs" && active && wiki.data && (
          <div className="hidden flex-col py-1 pl-4 lg:flex">
            {wiki.data.pages.filter((w) => w.exists !== false).map((w) => {
              const cur = (sub || "index") === w.name
              return (
                <a key={w.name} href={`#/docs/${w.name}`}
                   onClick={(e) => { e.preventDefault(); navigate("docs", w.name) }}
                   className={`rounded px-2 py-0.5 text-[12px] ${cur ? "text-foreground" : "text-muted-foreground hover:text-foreground"}`}>
                  {w.title}
                </a>
              )
            })}
          </div>
        )}
      </div>
    )
  })

  return (
    <nav
      aria-label="portal"
      className="flex shrink-0 flex-col gap-2 border-b bg-background px-2 py-2 lg:w-[214px] lg:overflow-y-auto lg:overscroll-contain lg:border-b-0 lg:border-r lg:px-3 lg:py-4"
    >
      <div className="flex gap-1 overflow-x-auto pb-1 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:pb-0">
        {items}
      </div>

      {/* Pinned with the navigation rather than the content: a keyed transmitter must stay on
          screen wherever the reader has scrolled to. */}
      {/* Was `hidden … lg:flex`, which took the jammer indicator off screen on any narrow window.
          A keyed transmitter is the one state this console must never conceal, so the block now
          shows at every width and simply lays out as a row when the rail is horizontal. */}
      <div className="mt-1 flex flex-row flex-wrap gap-x-4 gap-y-1.5 rounded-lg border bg-muted/40 px-3 py-2.5 font-mono text-[11px] lg:flex-col lg:gap-1.5">
        <div className="text-[9.5px] uppercase tracking-[2px] text-muted-foreground">state</div>
        <div className="flex items-center gap-2">
          <Dot tone={tele.liveness === "live" ? "good" : tele.liveness === "idle" ? "warn" : "bad"} pulse={tele.liveness === "live"} />
          agent {tele.liveness}
        </div>
        <div className="flex items-center gap-2">
          <Dot tone={roles.length === 0 ? "muted" : anyDown ? "bad" : "good"} />
          {roles.length === 0 ? "no radios" : roles.map(([r, d]) => `${r} ${d.alive ? "up" : "DOWN"}`).join(" · ")}
        </div>
        <div className="flex items-center gap-2">
          <Dot tone={!topo?.declared ? "muted" : topo.identity_verified === true ? "good" : topo.identity_verified === false ? "bad" : "warn"} />
          {!topo?.declared ? "no bench declared" : topo.identity_verified === true ? "bench verified" : topo.identity_verified === false ? "bench MISMATCH" : "bench unchecked"}
        </div>
        <div className="flex items-center gap-2">
          <Dot tone={!path ? "muted" : path.ok ? "good" : "bad"} />
          {!path ? "path not measured" : path.ok ? "path verified" : "path FAILED"}
        </div>
        <div className={`flex items-center gap-2 ${jam?.armed ? "font-semibold text-bad" : ""}`}>
          <Dot tone={jam?.armed ? "bad" : jam?.enabled ? "muted" : "muted"} pulse={!!jam?.armed} />
          {jam?.armed ? "JAMMER TRANSMITTING" : jam?.enabled ? "jammer idle" : "jammer disabled"}
        </div>
      </div>

      {/* Display preferences, pinned to the foot of the rail.
          These say nothing about the experiment — they are how this browser likes to look — so
          they do not belong in the header, where every millimetre is spent on what is being
          measured and where they pushed the state band onto a second line. `mt-auto` drops them
          to the bottom of the tall rail; on a narrow window the rail is a horizontal strip and
          they simply sit at its end. */}
      <div className="mt-auto flex shrink-0 items-center justify-end gap-1.5 pt-1 lg:justify-between lg:border-t lg:pt-3">
        <span className="hidden text-[9.5px] uppercase tracking-[2px] text-muted-foreground lg:inline">view</span>
        <span className="flex items-center gap-1.5">
          <DensityToggle />
          <ThemeToggle />
        </span>
      </div>
    </nav>
  )
}
