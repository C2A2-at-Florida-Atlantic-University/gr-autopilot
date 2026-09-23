import type { Telemetry } from "@/lib/telemetry"
import { PAGES } from "@/lib/router"
import { Devices } from "@/components/Devices"
import { PageHeader, Raw } from "@/components/PageHeader"
import { Card } from "@/components/ui/card"

const P = PAGES.find((p) => p.id === "devices")!

function Row({ k, v, tone }: { k: string; v: string; tone?: "good" | "bad" | "warn" }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-foreground"
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-x-3 border-t border-border/60 py-1.5 text-[12px] first:border-t-0">
      <span className="text-muted-foreground">{k}</span>
      <span className={`font-mono ${cls}`}>{v}</span>
    </div>
  )
}

export function DevicesPage({ tele }: { tele: Telemetry }) {
  const d = tele.devices
  const path = d?.path
  const jam = d?.jammer
  return (
    <div className="flex flex-col gap-3">
      <PageHeader title="Devices" endpoint={P.endpoint} hint="measured on every poll — a radio can disappear between one and the next"
                  error={tele.liveness === "offline" ? "daemon unreachable" : null} />
      <Devices devices={d} />

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <Card tier="tertiary" className="px-3.5 py-2.5">
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">Backend · radio process</div>
          <Row k="backend" v={d?.backend ?? "—"} />
          <Row k="worker" v={d?.worker ? `${d.worker.running ? "running" : "ended"} · pid ${d.worker.pid ?? "—"} · ${d.worker.reason}${d.worker.signal ? ` (${d.worker.signal})` : ""}` : "none (simulated)"}
               tone={d?.worker ? (d.worker.running ? "good" : "bad") : undefined} />
          {Object.entries(d?.devices ?? {}).map(([role, dev]) => (
            <Row key={role} k={role} v={`${dev.uri} · ${dev.alive ? "answering" : `DOWN — ${dev.reason ?? "not answering"}`}${dev.worker ? ` · worker ${dev.worker}` : ""}`}
                 tone={dev.alive ? "good" : "bad"} />
          ))}
          {d?.note && <Row k="note" v={d.note} />}
          {d?.error && <Row k="error" v={d.error} tone="bad" />}
        </Card>

        <Card tier="tertiary" className="px-3.5 py-2.5">
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">Path check · Tier B, by transmitting</div>
          {!path ? (
            <div className="text-[12px] text-muted-foreground">Not measured — start the daemon with <code className="font-mono text-foreground">--verify-path</code>. Identity is free to check; continuity costs a transmission.</div>
          ) : (
            <>
              <Row k="verdict" v={path.ok ? "verified" : "FAILED"} tone={path.ok ? "good" : "bad"} />
              <Row k="link" v={path.link_snr_db !== null ? `${path.link_snr_db} dB (BPSK)${path.expected_snr_db != null ? ` · calibration ${path.expected_snr_db} dB` : ""}` : "—"} tone={path.link_ok ? "good" : "bad"} />
              <Row k="link BER" v={path.link_ber !== null ? path.link_ber.toExponential(2) : "—"} />
              <Row k="jammer margin" v={path.jammer_margin_db !== null ? `+${path.jammer_margin_db} dB median${path.jammer_margin_spread_db != null ? ` · spread ${path.jammer_margin_spread_db} dB` : ""}` : "no jammer declared"}
                   tone={path.jammer_ok === null ? undefined : path.jammer_ok ? "good" : "bad"} />
              {path.findings.map((f) => <div key={f} className="mt-1.5 text-[12px] text-bad">{f}</div>)}
            </>
          )}
        </Card>

        <Card tier="tertiary" className="px-3.5 py-2.5">
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">Interferer · read-only here</div>
          {!jam ? <div className="text-[12px] text-muted-foreground">none</div> : (
            <>
              <Row k="state" v={jam.armed ? "TRANSMITTING" : jam.enabled ? (jam.available ? "idle" : "not found") : "disabled (start with --jammer)"} tone={jam.armed ? "bad" : jam.enabled ? "good" : undefined} />
              <Row k="intended" v={jam.intended ? "on" : "off"} tone={jam.intended && !jam.armed ? "bad" : undefined} />
              <Row k="frequency" v={`${(jam.center_freq_hz / 1e6).toFixed(3)} MHz`} />
              <Row k="if gain" v={`${jam.if_gain} dB · ${jam.kind}${jam.follow ? " · following" : ""}`} />
              {jam.retune_count !== undefined && <Row k="retunes" v={`${jam.retune_count}${jam.last_retune_latency_s != null ? ` · last ${(jam.last_retune_latency_s * 1e3).toFixed(1)} ms` : ""}`} />}
              {jam.error && <Row k="error" v={jam.error} tone="bad" />}
              <a href="#/operator" className="mt-1.5 inline-block font-mono text-[11px] text-primary">arm / move / disarm on the Operator page →</a>
            </>
          )}
        </Card>
      </div>
      {d && <Raw data={d} />}
    </div>
  )
}
