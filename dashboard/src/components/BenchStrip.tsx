import type { Telemetry } from "@/lib/telemetry"
import { IdentityVerdict, PathVerdict, CHIP } from "@/components/Verdicts"

/** One line on the overview: the bench, at a glance, each item a link to its page. */
export function BenchStrip({ tele }: { tele: Telemetry }) {
  const devs = Object.entries(tele.devices?.devices ?? {})
  const jam = tele.devices?.jammer
  const topo = tele.topology
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border bg-muted/40 px-3 py-2 text-[11px]">
      <a href="#/devices" className="flex items-center gap-2 hover:text-primary">
        <span className="uppercase tracking-wider text-muted-foreground">radios</span>
        {devs.length === 0
          ? <span className="text-muted-foreground">{tele.devices?.backend ?? "—"}</span>
          : devs.map(([r, d]) => (
              <span key={r} className="inline-flex items-center gap-1 font-mono">
                <span className={`h-2 w-2 rounded-full ${d.alive ? "bg-good" : "bg-bad"}`} />{r}
              </span>
            ))}
      </a>
      <a href="#/topology" className="flex items-center gap-2 hover:text-primary">
        <span className="uppercase tracking-wider text-muted-foreground">bench</span>
        {topo?.declared ? <IdentityVerdict verified={topo.identity_verified} /> : <span className="text-muted-foreground">none declared</span>}
        <PathVerdict path={tele.devices?.path} />
      </a>
      <a href="#/operator" className="ml-auto flex items-center gap-2 hover:text-primary">
        <span className="uppercase tracking-wider text-muted-foreground">interferer</span>
        {jam?.armed
          ? <span className={`${CHIP} bg-bad/15 text-bad animate-pulse`}>transmitting · {(jam.center_freq_hz / 1e6).toFixed(1)} MHz</span>
          : <span className={`${CHIP} bg-muted text-muted-foreground`}>{jam?.enabled ? "idle" : "disabled"}</span>}
      </a>
    </div>
  )
}
