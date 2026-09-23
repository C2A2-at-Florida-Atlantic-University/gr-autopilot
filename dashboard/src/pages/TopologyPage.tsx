import type { Telemetry } from "@/lib/telemetry"
import { PAGES } from "@/lib/router"
import { TopologyGraph } from "@/components/TopologyGraph"
import { IdentityVerdict, PathVerdict, CHIP } from "@/components/Verdicts"
import { PageHeader, Empty, Raw } from "@/components/PageHeader"
import { Card } from "@/components/ui/card"

const P = PAGES.find((p) => p.id === "topology")!

export function TopologyPage({ tele }: { tele: Telemetry }) {
  const t = tele.topology
  const path = tele.devices?.path
  if (!t) return (
    <div className="flex flex-col gap-3">
      <PageHeader title="Topology" endpoint={P.endpoint} hint={P.hint} error={tele.liveness === "offline" ? "daemon unreachable" : null} />
      <Empty>Waiting for the daemon…</Empty>
    </div>
  )
  if (!t.declared) return (
    <div className="flex flex-col gap-3">
      <PageHeader title="Topology" endpoint={P.endpoint} hint={P.hint} />
      <Empty>{t.note ?? "No topology declared — device identity is unverifiable."} Declare the bench in <code className="font-mono">config/bench.yaml</code> and restart.</Empty>
    </div>
  )
  const att = (t.attestation ?? {}) as Record<string, unknown>
  const repro = t.reproducibility
  const geo = t.geometry
  const band = t.link_band_hz

  return (
    <div className="flex flex-col gap-3">
      <PageHeader title="Topology" endpoint={P.endpoint} hint={`${t.name} · ${t.source}`}>
        {t.sha256 && <span className="opacity-60">sha {t.sha256.slice(0, 12)}</span>}
      </PageHeader>

      <div className="flex flex-wrap items-center gap-2">
        <IdentityVerdict verified={t.identity_verified} />
        <PathVerdict path={path} />
        <span className={`${CHIP} ${t.jammer_permitted ? "bg-warn/15 text-warn" : "bg-muted text-muted-foreground"}`}>
          jammer {t.jammer_permitted ? "permitted" : "refused"}
        </span>
        {repro && <span className={`${CHIP} ${repro.recorded ? "bg-good/15 text-good" : "bg-warn/15 text-warn"}`}>{repro.recorded ? "reproducible" : "not reproducible"}</span>}
        {band && <span className="ml-auto font-mono text-[11px] text-muted-foreground">link band {(band[0] / 1e6).toFixed(0)}–{(band[1] / 1e6).toFixed(0)} MHz</span>}
      </div>

      <Card tier="primary" className="p-2">
        <TopologyGraph topology={t} devices={tele.devices} path={path} />
      </Card>

      {((t.findings && t.findings.length > 0) || (path && path.findings.length > 0)) && (
        <Card tier="tertiary" className="border-bad/40 px-3.5 py-2.5">
          <div className="mb-1 text-[10px] uppercase tracking-wider text-bad">findings</div>
          <ul className="space-y-0.5 text-[12px] text-bad">
            {(t.findings ?? []).map((f) => <li key={f}>{f}</li>)}
            {(path?.findings ?? []).map((f) => <li key={f}>{f}</li>)}
          </ul>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <Card tier="tertiary" className="px-3.5 py-2.5">
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">Declared devices</div>
          <table className="w-full text-[12px]">
            <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr><th className="py-1 text-left font-medium">name</th><th className="text-left font-medium">role</th><th className="text-left font-medium">kind · chip</th><th className="text-left font-medium">uri</th><th className="text-left font-medium">serial</th></tr>
            </thead>
            <tbody className="font-mono">
              {Object.entries(t.devices ?? {}).map(([n, d]) => (
                <tr key={n} className="border-t border-border/60">
                  <td className="py-1 text-foreground">{n}</td><td>{d.role}</td>
                  <td>{d.kind}{d.expect_chip ? ` · ${d.expect_chip}` : ""}</td>
                  <td className="text-muted-foreground">{d.uri ?? "usb"}</td>
                  <td className="text-muted-foreground" title={d.expect_serial ?? ""}>{d.expect_serial ? `…${d.expect_serial.slice(-12)}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2 text-[10px] uppercase tracking-wider text-muted-foreground">Declared path</div>
          <ul className="font-mono text-[12px]">
            {(t.path ?? []).map((s, i) => (
              <li key={i} className="border-t border-border/60 py-1">
                {s.from} <span className="text-muted-foreground">—{s.medium.replace("_", " ")}{s.loss_db != null ? ` ${s.loss_db} dB` : ""}→</span> {s.to}
              </li>
            ))}
          </ul>
        </Card>

        <Card tier="tertiary" className="px-3.5 py-2.5">
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">Attestation · Tier C, the operator's word</div>
          <div className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1 text-[12px]">
            <span className="text-muted-foreground">medium</span><span className="font-mono">{String(att.medium ?? "—")}{att.shielded === true ? " · shielded" : att.shielded === false ? " · NOT shielded" : ""}</span>
            <span className="text-muted-foreground">antennas</span><span className="font-mono">{att.antennas_attached === true ? "attached" : att.antennas_attached === false ? "none (cabled)" : "—"}</span>
            <span className="text-muted-foreground">operator</span><span className="font-mono">{String(att.operator ?? "—")}{att.date ? ` · ${String(att.date)}` : ""}</span>
            <span className="text-muted-foreground">jammer</span><span className="font-mono">{t.jammer_permitted_reason ?? "—"}</span>
          </div>
          {att.statement != null && <p className="mt-2 border-l-2 border-border pl-2 text-[12px] italic text-muted-foreground">{String(att.statement)}</p>}

          <div className="mt-3 text-[10px] uppercase tracking-wider text-muted-foreground">Geometry</div>
          <div className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1 text-[12px]">
            <span className="text-muted-foreground">TX–RX separation</span><span className={`font-mono ${geo?.separation_m == null ? "text-warn" : ""}`}>{geo?.separation_m != null ? `${geo.separation_m} m` : "unrecorded"}</span>
            <span className="text-muted-foreground">JAM–RX separation</span><span className={`font-mono ${geo?.jammer_separation_m == null ? "text-warn" : ""}`}>{geo?.jammer_separation_m != null ? `${geo.jammer_separation_m} m` : "unrecorded"}</span>
          </div>
          {geo?.antennas && <p className="mt-1.5 text-[11.5px] text-muted-foreground">{geo.antennas}</p>}
          {repro && !repro.recorded && <p className="mt-2 text-[12px] text-warn">Unrecorded: {repro.missing.join(", ")} — {repro.note}.</p>}
        </Card>
      </div>
      <Raw data={t} />
    </div>
  )
}
