import type { DevicesPayload } from "@/types"
import { Card } from "@/components/ui/card"

/**
 * One tile per radio, showing whether it is answering RIGHT NOW.
 *
 * The state is measured on every poll rather than reported once at startup, because the whole
 * point is that a radio can disappear between one poll and the next. A device that stops
 * answering is shown as down, by name, while the others stay up -- losing the receiver must not
 * blank a perfectly healthy transmitter.
 */
export function Devices({ devices }: { devices: DevicesPayload | null }) {
  if (!devices) return null

  const entries = Object.entries(devices.devices ?? {})
  if (entries.length === 0) {
    return (
      <Card tier="tertiary" className="shrink-0 px-3.5 py-2.5">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted-foreground">
          <span>Radios</span>
          <span className="normal-case tracking-normal">
            {devices.note ?? "no radios attached — simulated backend"}
          </span>
        </div>
      </Card>
    )
  }

  const workerDead = devices.worker && !devices.worker.running

  return (
    <Card tier="tertiary" className="shrink-0 px-3.5 py-2.5">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span>Radios · live state</span>
        {workerDead && (
          <span className="normal-case tracking-normal text-bad">
            radio process ended{devices.worker?.signal ? ` (${devices.worker.signal})` : ""} — no
            measurements until it is restarted
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-2">
        {entries.map(([role, d]) => (
          <div
            key={role}
            className={`flex min-w-[190px] flex-1 items-center gap-2.5 rounded-lg border px-3 py-2 ${
              d.alive ? "border-good/30 bg-good/5" : "border-bad/40 bg-bad/10"
            }`}
          >
            <span
              className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                d.alive ? "bg-good" : "bg-bad"
              }`}
            />
            <div className="min-w-0">
              <div className="font-mono text-[12px] font-semibold uppercase">{role}</div>
              <div className="truncate font-mono text-[10.5px] text-muted-foreground">{d.uri}</div>
            </div>
            <div className="ml-auto shrink-0 text-right">
              {d.alive ? (
                <span className="font-mono text-[11px] text-good">
                  {d.temp_c !== undefined ? `${d.temp_c}°C` : "OK"}
                </span>
              ) : (
                <span className="font-mono text-[11px] text-bad">DOWN</span>
              )}
            </div>
          </div>
        ))}
      </div>
      {entries.some(([, d]) => !d.alive) && (
        <div className="mt-2 text-[11px] text-muted-foreground">
          {entries
            .filter(([, d]) => !d.alive)
            .map(([role, d]) => `${role}: ${d.reason ?? "not answering"}`)
            .join(" · ")}
        </div>
      )}
    </Card>
  )
}

/** Which roles are currently unusable. Used to blank the panels that depend on them. */
export function downRoles(devices: DevicesPayload | null): string[] {
  if (!devices) return []
  return Object.entries(devices.devices ?? {})
    .filter(([, d]) => !d.alive)
    .map(([role]) => role)
}
