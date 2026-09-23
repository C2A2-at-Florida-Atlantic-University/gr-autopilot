import { useEffect, useState } from "react"
import type { Telemetry } from "@/lib/telemetry"
import { PAGES } from "@/lib/router"
import { accessToken, postControl, readControl, setAccessToken } from "@/lib/operator"
import { Jammer } from "@/components/Jammer"
import { Controls } from "@/components/Controls"
import { PageHeader, Raw } from "@/components/PageHeader"
import { Card } from "@/components/ui/card"

const P = PAGES.find((p) => p.id === "operator")!
const BTN = "inline-flex items-center gap-1.5 rounded-md border px-3 py-1 text-[12px] font-medium transition-colors hover:bg-muted disabled:opacity-40"

/**
 * Everything the agent must not be able to do, in one place: the hidden channel (transmit
 * attenuation, receive gain), the interferer, and — when a scripted console is driving — the
 * scenario. All of it goes through POST /control, behind the daemon's bearer token.
 */
export function OperatorPage({ tele }: { tele: Telemetry }) {
  const [token, setToken] = useState(accessToken())
  const [state, setState] = useState<Record<string, unknown> | null>(null)
  const [atten, setAtten] = useState<string>("")
  const [gain, setGain] = useState<string>("")
  const [msg, setMsg] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let alive = true
    readControl().then((s) => {
      if (!alive) return
      setState(s)
      if (s) {
        if (typeof s.tx_atten_db === "number" && atten === "") setAtten(String(s.tx_atten_db))
        if (typeof s.rx_gain_db === "number" && gain === "") setGain(String(s.rx_gain_db))
      }
    })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, token])

  const send = async (cmd: Record<string, unknown>) => {
    const e = await postControl(cmd)
    setMsg(e ?? "applied")
    setNonce((n) => n + 1)
  }

  const hasToken = token.length > 0
  return (
    <div className="flex flex-col gap-3">
      <PageHeader title="Operator" endpoint={P.endpoint} hint="the integrity boundary: what the agent cannot see or set" error={tele.liveness === "offline" ? "daemon unreachable" : null}>
        {state ? <span className="text-good">control reachable</span> : hasToken ? <span className="text-warn">token not verified</span> : <span className="text-warn">not verified</span>}
      </PageHeader>

      <Card tier="tertiary" className="px-3.5 py-2.5">
        <div className="mb-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">Access token · the daemon's --token; leave blank on the bench host (loopback is exempt)</div>
        <div className="flex flex-wrap items-center gap-2">
          <input type="password" value={token} autoComplete="off"
                 placeholder="the daemon's --token value (remote only)"
                 onChange={(e) => { setToken(e.target.value); setAccessToken(e.target.value) }}
                 className="w-[28rem] max-w-full rounded-md border bg-muted px-2 py-1 font-mono text-[12px]" aria-label="access token" />
          <button className={BTN} onClick={() => setNonce((n) => n + 1)}>Check</button>
          <span className="text-[11px] text-muted-foreground">kept in this browser only</span>
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <Card tier="secondary" className="px-3.5 py-2.5">
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">Hidden channel · the agent reasons from measurements, never from these</div>
          <div className="flex flex-col gap-2">
            <label className="flex flex-wrap items-center gap-2 text-[12px]">
              <span className="w-32 text-muted-foreground">TX attenuation</span>
              <input type="number" step={1} min={0} max={89} value={atten} onChange={(e) => setAtten(e.target.value)}
                     className="w-24 rounded-md border bg-muted px-2 py-1 font-mono text-[12px]" aria-label="tx_atten_db" />
              <span className="font-mono text-muted-foreground">dB</span>
              <button className={BTN} disabled={!hasToken || atten === ""} onClick={() => send({ tx_atten_db: Number(atten) })}>Apply</button>
              <span className="font-mono text-[11px] text-muted-foreground">higher = harder link · ~0.78 dB SNR per dB</span>
            </label>
            <label className="flex flex-wrap items-center gap-2 text-[12px]">
              <span className="w-32 text-muted-foreground">RX gain</span>
              <input type="number" step={1} min={0} max={73} value={gain} onChange={(e) => setGain(e.target.value)}
                     className="w-24 rounded-md border bg-muted px-2 py-1 font-mono text-[12px]" aria-label="rx_gain_db" />
              <span className="font-mono text-muted-foreground">dB</span>
              <button className={BTN} disabled={!hasToken || gain === ""} onClick={() => send({ rx_gain_db: Number(gain) })}>Apply</button>
              <span className="font-mono text-[11px] text-muted-foreground">calibrated optimum 45 · stable 35–50</span>
            </label>
            {msg && <div className={`font-mono text-[11px] ${msg === "applied" ? "text-good" : "text-bad"}`}>{msg}</div>}
            {state && (typeof state.tx_atten_db === "number" || typeof state.rx_gain_db === "number") && (
              <div className="font-mono text-[11px] text-muted-foreground">
                now: tx_atten {String(state.tx_atten_db ?? "—")} dB · rx_gain {String(state.rx_gain_db ?? "—")} dB
              </div>
            )}
          </div>
        </Card>

        <div className="flex flex-col gap-3">
          <Jammer status={tele.devices?.jammer} />
          {tele.snapshot?.control && <Controls control={tele.snapshot.control} />}
        </div>
      </div>

      <p className="max-w-[80ch] text-[11.5px] text-muted-foreground">
        The agent reaches <code className="font-mono">/mcp</code> on this same port and holds the same token, so this page is not a barrier to it —
        what it cannot get from here is the hidden channel quality, which <code className="font-mono">GET /control</code> does not report to anyone.
      </p>
      {state && <Raw data={state} />}
    </div>
  )
}
