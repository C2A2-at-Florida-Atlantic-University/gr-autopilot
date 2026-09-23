import { useState } from "react"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Separator } from "@/components/ui/separator"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ConfirmDialog } from "@/components/ui/sheet"
import { Term } from "@/components/ui/tooltip"
import { Glyph } from "@/components/Glyph"
import { postControl } from "@/lib/operator"
import type { Control } from "@/types"

const TARGETS = [1e-1, 1e-2, 1e-3]

/**
 * The operator surface: start/pause, reset, target BER, and the interferer.
 *
 * Design language L4 — the operator surface and the agent surface never blend. The agent's own
 * structural control stays on the MCP tool surface; this only sets the scenario. The one control
 * here that radiates gets a confirmation naming what it will do, because "are you sure?" is not
 * a description of a transmitter about to key in a shared chamber.
 */
export function Controls({ control }: { control: Control }) {
  const [running, setRunning] = useState(control.running)
  const [jammer, setJammer] = useState(control.jammer)
  const [target, setTarget] = useState(control.target_ber)
  const [confirmJam, setConfirmJam] = useState(false)

  const armJammer = () => {
    setJammer(true)
    postControl({ jammer: true }, "Jammer armed — the interferer is transmitting")
  }

  return (
    <Card className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2">
      <span className="font-mono text-[10px] font-semibold uppercase tracking-[2px] text-muted-foreground">
        operator
      </span>

      <Button
        variant={running ? "default" : "primary"}
        size="sm"
        onClick={() => {
          const v = !running
          setRunning(v)
          postControl({ running: v }, v ? "Run resumed" : "Run paused")
        }}
      >
        <Glyph name={running ? "pause" : "play"} size={12} />{running ? "Pause" : "Start"}
      </Button>

      <Button variant="ghost" size="sm" onClick={() => postControl({ reset: true }, "Scenario reset")}>
        <Glyph name="reset" size={12} />Reset
      </Button>

      <Separator orientation="vertical" className="h-5" />

      <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
        <Term hint="The error ratio the link must meet. The agent maximises spectral efficiency subject to holding this.">
          target BER
        </Term>
        <Select
          value={String(target)}
          onValueChange={(v) => { setTarget(+v); postControl({ target_ber: +v }, `Target BER set to ${(+v).toExponential(0)}`) }}
        >
          <SelectTrigger className="h-7 w-[92px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            {TARGETS.map((t) => <SelectItem key={t} value={String(t)}>{t.toExponential(0)}</SelectItem>)}
          </SelectContent>
        </Select>
      </label>

      <Separator orientation="vertical" className="h-5" />

      <label className={`flex items-center gap-2 text-[12px] ${jammer ? "font-semibold text-bad" : "text-muted-foreground"}`}>
        <Switch
          tone="danger"
          checked={jammer}
          onCheckedChange={(v) => {
            if (v) { setConfirmJam(true); return }   // arming is confirmed; disarming is not
            setJammer(false)
            postControl({ jammer: false }, "Jammer disarmed")
          }}
          aria-label="Arm the interferer"
        />
        {jammer ? "JAMMER TRANSMITTING" : "jammer idle"}
      </label>

      <ConfirmDialog
        open={confirmJam}
        onOpenChange={setConfirmJam}
        title="Arm the interferer?"
        body={
          <>
            This keys a real HackRF inside the chamber. Anything else sharing that space — including
            a neighbouring experiment — will see it. Arming is gated on the bench attestation in{" "}
            <code className="font-mono text-[12px] text-foreground">config/bench.yaml</code>; the daemon
            refuses if the path is not declared contained.
          </>
        }
        confirmLabel="Arm and transmit"
        onConfirm={armJammer}
      />
    </Card>
  )
}
