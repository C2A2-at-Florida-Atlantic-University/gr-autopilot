import type { Telemetry } from "@/lib/telemetry"
import { Plot } from "@/components/Plot"
import { Constellation } from "@/components/Constellation"
import { Spectrum } from "@/components/Spectrum"
import { Waterfall } from "@/components/Waterfall"
import { Trends } from "@/components/Trends"
import { Occupancy } from "@/components/Occupancy"
import { Tiles } from "@/components/Tiles"
import { Controls } from "@/components/Controls"
import { Transport } from "@/components/Transport"
import { downRoles } from "@/components/Devices"
import { BenchStrip } from "@/components/BenchStrip"
import { ChannelStrip } from "@/components/ChannelStrip"
import { Card } from "@/components/ui/card"
import { linkState } from "@/lib/link"
import { openExperimentDialog } from "@/App"

/**
 * Shown in place of a plot whose radio is gone. The constellation and the spectrum are both made
 * from what the RECEIVER captured; if the receiver is not there, the last picture drawn is
 * history, and leaving it up invites reading an old capture as the current state of the link.
 */
function PanelUnavailable({ role, reason }: { role: string; reason?: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1.5 text-center">
      <div className="font-mono text-[12px] font-semibold text-bad">{role.toUpperCase()} RADIO UNAVAILABLE</div>
      <div className="max-w-[38ch] text-[11.5px] text-muted-foreground">{reason ?? "not answering"}</div>
      <div className="max-w-[38ch] text-[11px] text-muted-foreground">Nothing is being captured, so no plot is shown.</div>
    </div>
  )
}

function LegendDot({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className={`h-2 w-2 rounded-[2px] ${className}`} />
      {label}
    </span>
  )
}

export function Overview({ tele }: { tele: Telemetry }) {
  const s = tele.snapshot
  const down = downRoles(tele.devices)
  const rxDown = down.includes("rx")
  const rxReason = tele.devices?.devices?.rx?.reason
  // "Nothing is running right now" — NOT "there is nothing to show". The frames stay up, dimmed;
  // the LIVE/IDLE/OFFLINE badge in the header is what states which of the two this is, and it
  // carries the age with it, so a second banner saying the same thing is only noise.
  const stale = tele.mode === "live" && tele.liveness !== "live"
  // What the ledger says about the channel: where the link is, where it moved from, and what the
  // last silent sweep measured there. None of it is in the snapshot — it happens between trials.
  const link = linkState(tele.ledger, s)
  // The sweep is published in the snapshot only by the script drivers; over the MCP tool surface
  // it reaches the page through the ledger, so the panel takes whichever arrived.
  const occupancy = s?.occupancy?.length ? s.occupancy : link.lastSense?.channels

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2.5">
      {tele.mode === "replay" && s && <Transport tele={tele} />}
      {tele.mode === "live" && <BenchStrip tele={tele} />}
      {s && <ChannelStrip link={link} />}
      {s?.control && <Controls control={s.control} />}

      {s ? (
        <div className={`flex min-h-0 flex-1 flex-col gap-2.5 ${stale ? "opacity-60 saturate-[0.7]" : ""}`}>
          <Tiles snapshot={s} rows={tele.ledger} />

          {/* The plot field. With the ledger in its own rail this owns the whole remaining
              height, so the grid rows share it evenly instead of collapsing to min-height. */}
          <div className="grid min-h-0 flex-1 grid-cols-1 grid-rows-2 gap-2.5 lg:grid-cols-2">
            <Plot title="Constellation — recovered symbols">
              {rxDown ? <PanelUnavailable role="rx" reason={rxReason} /> : <Constellation points={s.constellation} />}
            </Plot>
            {/* No centre frequency in either title: it is on the centre tick of the axis itself,
                where it stays attached to the picture if the panel is cropped or screenshotted. */}
            <Plot title="Spectrum — PSD (dB)">
              {rxDown ? <PanelUnavailable role="rx" reason={rxReason} />
                      : <Spectrum spectrum={s.spectrum} evidence={link.interferer} />}
            </Plot>
            <Plot title="Waterfall — spectrum history, newest at top">
              {rxDown ? <PanelUnavailable role="rx" reason={rxReason} />
                      : <Waterfall spectrum={s.spectrum} t={s.t} freqHz={link.freqHz}
                                   experimentKey={tele.experiment?.slug ?? ""} />}
            </Plot>
            <Plot title="Trends — BER · SNR · EVM per trial">
              <Trends rows={tele.ledger} targetBer={s.metrics.target_ber} />
            </Plot>
          </div>

          {occupancy && occupancy.length > 0 && (
            <Card tier="tertiary" className="flex h-[170px] shrink-0 flex-col px-3.5 pb-3 pt-2.5">
              <figcaption className="mb-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[10px] uppercase tracking-wider text-muted-foreground">
                <span>
                  Spectrum sensing — channel occupancy · monitor role (link silent)
                  {link.lastSense && !s.occupancy?.length ? ` · sweep #${link.lastSense.iteration}` : ""}
                </span>
                <span className="flex items-center gap-3 normal-case tracking-normal">
                  <LegendDot className="bg-bad" label="jammed" />
                  <LegendDot className="bg-good/50" label="clear" />
                  <LegendDot className="bg-primary" label="link" />
                </span>
              </figcaption>
              <div className="min-h-0 flex-1">
                <Occupancy channels={occupancy} currentFreqHz={link.freqHz ?? undefined}
                           thresholdDb={occupancy[0]?.detect_margin_db ?? 3} />
              </div>
            </Card>
          )}
        </div>
      ) : tele.mode === "live" && tele.connected && tele.experiment === null ? (
        <div className="flex shrink-0 flex-wrap items-center justify-center gap-3 rounded-xl border border-warn/40 bg-warn/10 p-5 text-sm">
          <span className="font-mono font-semibold text-warn">NO EXPERIMENT SELECTED</span>
          <span className="text-muted-foreground">Nothing can be measured or recorded until one is named — from here, or by the agent asking you.</span>
          <button onClick={openExperimentDialog}
                  className="rounded-md border border-warn/50 bg-warn/15 px-3 py-1 font-mono text-[12px] font-medium text-warn hover:bg-warn/25">
            name it
          </button>
        </div>
      ) : (
        <div className="shrink-0 rounded-xl border bg-card p-5 text-center text-sm text-muted-foreground">
          No measurement has been taken yet — the plots appear after the first{" "}
          <code className="mx-1 font-mono text-primary">run_flowgraph</code>. The history in the rail
          is readable regardless.
        </div>
      )}
    </div>
  )
}
