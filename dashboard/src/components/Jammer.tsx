import { useState } from "react"
import { Card } from "@/components/ui/card"
import { Glyph } from "@/components/Glyph"
import type { JammerStatus } from "@/types"
import { accessToken, postControl, setAccessToken } from "@/lib/operator"

/**
 * Arm, move and disarm the framework's interferer.
 *
 * The jammer is operator property and is deliberately absent from the agent's tool surface — an
 * agent told where the interference is has not adapted to anything. The dashboard is therefore the
 * only place a human can see it, which makes the TRANSMITTING indicator the safety-critical part
 * of this component: a jammer left keyed silently poisons every later measurement, and the band
 * simply reads busy with nothing to say why.
 *
 * Two states are shown separately on purpose. `intended` is what the operator asked for; `armed`
 * is what the hardware is doing. When they disagree the jammer started and then died, which looks
 * exactly like a clean band unless something says otherwise.
 */
const BTN = "inline-flex items-center gap-1.5 rounded-md border px-3 py-1 text-[12px] font-medium transition-colors hover:bg-muted disabled:opacity-40 disabled:hover:bg-transparent"

export function Jammer({ status }: { status: JammerStatus | undefined }) {
  const [freqMhz, setFreqMhz] = useState(() => (status?.center_freq_hz ?? 2.37e9) / 1e6)
  const [gain, setGain] = useState(status?.if_gain ?? 20)
  const [follow, setFollow] = useState(status?.follow ?? false)
  const [err, setErr] = useState<string | null>(null)
  const [token, setToken] = useState(accessToken())

  if (!status) return null

  const send = async (cmd: Record<string, unknown>) => {
    const e = await postControl(cmd)
    setErr(e)
  }

  // A daemon started without --jammer refuses every command; say so rather than offering
  // controls that cannot work.
  if (!status.enabled) {
    return (
      <Card tier="tertiary" className="shrink-0 px-3.5 py-2">
        <span className="text-[10px] font-semibold uppercase tracking-[2px] text-muted-foreground">
          jammer
        </span>
        <span className="ml-3 text-[12px] text-muted-foreground">
          disabled — restart the daemon with <code>--jammer</code>, and the bench declaration must
          attest a contained path
        </span>
      </Card>
    )
  }

  const died = status.intended && !status.armed

  return (
    <Card
      tier="tertiary"
      className={`shrink-0 px-3.5 py-2.5 ${status.armed ? "border-bad/50" : ""}`}
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <span className="text-[10px] font-semibold uppercase tracking-[2px] text-muted-foreground">
          jammer
        </span>

        {/* The indicator, not the button, is the point of this panel. */}
        {status.armed && (
          <span className="inline-flex items-center gap-1.5 rounded bg-bad/15 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-bad">
            <span className="h-2 w-2 animate-pulse rounded-full bg-bad" />
            transmitting · {(status.center_freq_hz / 1e6).toFixed(1)} MHz
          </span>
        )}
        {died && (
          <span className="rounded bg-warn/15 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-warn">
            armed but not transmitting
          </span>
        )}

        <button
          onClick={() => send({
            jammer: !status.armed,
            jammer_freq_hz: freqMhz * 1e6,
            jammer_if_gain: gain,
            jammer_follow: follow,
          })}
          className={`${BTN} ${status.armed ? "border-good/40 text-good" : "border-bad/40 text-bad"}`}
        >
          <Glyph name={status.armed ? "pause" : "play"} size={12} />
          {status.armed ? "Disarm" : "Arm"}
        </button>

        <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
          MHz
          <input
            type="number" step="0.1" value={freqMhz}
            onChange={(e) => setFreqMhz(Number(e.target.value))}
            className="w-24 rounded border bg-transparent px-1.5 py-0.5 text-[12px] tabular-nums"
          />
        </label>

        <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
          gain
          <input
            type="number" min={0} max={47} value={gain}
            onChange={(e) => setGain(Number(e.target.value))}
            className="w-16 rounded border bg-transparent px-1.5 py-0.5 text-[12px] tabular-nums"
          />
        </label>

        <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
          <input
            type="checkbox" checked={follow}
            onChange={(e) => { setFollow(e.target.checked); if (status.armed) send({ jammer_follow: e.target.checked }) }}
          />
          follow the link
        </label>

        {status.retune_count != null && status.retune_count > 0 && (
          <span className="text-[12px] tabular-nums text-muted-foreground">
            {status.retune_count} retunes · {status.last_retune_latency_s}s
          </span>
        )}

        {/* Blank on the bench host: loopback is exempt from the bearer token. It is only
            needed when the console is open on another machine. */}
        <label className="ml-auto flex items-center gap-1.5 text-[12px] text-muted-foreground">
          token
          <input
            type="password" value={token} placeholder="--token (remote only)"
            onChange={(e) => { setToken(e.target.value); setAccessToken(e.target.value) }}
            className="w-36 rounded border bg-transparent px-1.5 py-0.5 font-mono text-[11px]"
          />
        </label>
      </div>

      {status.error && <p className="mt-1.5 text-[12px] text-bad">{status.error}</p>}
      {err && <p className="mt-1.5 text-[12px] text-bad">{err}</p>}
    </Card>
  )
}
