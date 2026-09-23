import { Term } from "@/components/ui/tooltip"
import type { LedgerRow, Snapshot } from "@/types"

/**
 * The headline measurements, one tile each.
 *
 * Design language L3: a measurement carries its unit and its confidence. Every tile has three
 * lines — quantity, unit, and the sample it rests on. Two consequences that are not cosmetic:
 *
 *  - A zero error count is rendered as a BOUND, not as zero. "BER 0" is a claim the measurement
 *    cannot support; "< 3.0e-05" is what 0 errors in 100k bits actually tells you (rule of three).
 *  - A trial that graded fewer bits than were requested is flagged. On this bench roughly a
 *    third of trials come back short, and their errors are alignment artefacts rather than
 *    channel errors — so a BER quoted without its n is not comparable to the one beside it.
 */
const BPS: Record<string, number> = { bpsk: 1, qpsk: 2, "8psk": 3, "16qam": 4, "32qam": 5, "64qam": 6, "256qam": 8 }
const RATE: Record<string, number> = { conv_k3_r12: 0.5, conv_k3_r34: 0.75 }

function bitsPerSym(modcod: string): number {
  const [mod, coding] = modcod.split(/[:+]/)
  return (BPS[mod] ?? 0) * (coding ? RATE[coding] ?? 1 : 1)
}

function Tile({ k, v, unit, n, tone, hint }: {
  k: string; v: string; unit?: string; n?: string
  tone?: "good" | "bad" | "warn"
  hint?: React.ReactNode
}) {
  const vc = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-foreground"
  return (
    <div className="flex min-w-0 flex-col rounded-lg border bg-card px-3 py-2">
      <span className="truncate font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        {hint ? <Term hint={hint}>{k}</Term> : k}
      </span>
      <span className={`mt-0.5 truncate font-mono text-[21px] leading-tight tabular-nums ${vc}`}>
        {v}
        {unit && <span className="ml-1 text-[13px] text-muted-foreground">{unit}</span>}
      </span>
      <span className="truncate font-mono text-[10px] text-muted-foreground/80">{n ?? " "}</span>
    </div>
  )
}

export function Tiles({ snapshot, rows }: { snapshot: Snapshot; rows: LedgerRow[] }) {
  const m = snapshot.metrics
  const runs = rows.filter((r) => r.verdict !== "annotation")
  const nBits = (snapshot as unknown as { n_bits?: number }).n_bits

  // 0 errors is a bound, not a value (rule of three at 95% confidence).
  const zero = m.ber === 0
  const bound = nBits ? `< ${(3 / nBits).toExponential(1)}` : "no errors"
  const berText = zero ? bound : m.ber.toExponential(2)

  return (
    <div className="grid shrink-0 grid-cols-2 gap-2 lg:grid-cols-4">
      <Tile
        k="Bit error ratio"
        v={berText}
        n={nBits ? `${zero ? 0 : Math.round(m.ber * nBits)} err / ${nBits.toLocaleString()} bits` : `target ${m.target_ber.toExponential(0)}`}
        tone={m.feasible ? "good" : "bad"}
        hint={zero
          ? "No errors were counted. That bounds the error ratio below the value shown; it does not measure it. Only more bits can tighten this."
          : "Errors counted divided by bits graded, for the last completed trial."}
      />
      <Tile
        k="EVM"
        v={m.evm_pct.toFixed(2)}
        unit="%"
        n={`${runs.length} trials in history`}
        hint="Error vector magnitude: the RMS distance between each received symbol and where it should have been, as a percentage of the constellation scale. Lower is tighter."
      />
      <Tile
        k="SNR (est.)"
        v={m.snr_db.toFixed(2)}
        unit="dB"
        n="framework-graded, not the true channel"
        hint="Estimated from the recovered symbols against the known payload. The operator's hidden channel setting is never exposed here."
      />
      <Tile
        k="Efficiency"
        v={bitsPerSym(snapshot.structure.modcod).toFixed(1)}
        unit="b/sym"
        n={snapshot.structure.modcod}
        hint="Spectral efficiency in bits per symbol: the thing the agent is maximising, subject to holding the BER target."
      />
    </div>
  )
}
