/**
 * Where the link is, and what the band under it actually measured.
 *
 * The snapshot is written once per graded trial, so it cannot carry anything the agent did
 * BETWEEN trials — a retune, a sweep, a diagnosis. Those are in the ledger, which `/data` serves
 * on the same poll, and this module reads them back out. Everything here is derived from rows the
 * framework wrote; nothing is inferred from the shape of a trace.
 *
 * That distinction is the point. An interferer flag drawn because a bin looked tall is a guess
 * dressed as a measurement; one drawn because an energy-detection sweep put that channel above
 * its detection threshold with the link silent is evidence, and it can state its own margin.
 */
import type { LedgerRow, OccupancyChannel, Snapshot } from "@/types"

export interface RetuneEvent {
  iteration: number
  toHz: number
  fromHz: number | null
}

export interface SenseEvent {
  iteration: number
  channels: OccupancyChannel[]
  /** Where the link was parked when the sweep was taken. */
  atHz: number | null
}

/** Why a channel is being called occupied, in terms a plot can print beside the flag. */
export interface InterfererEvidence {
  /** Received power over the noise floor, link silent, on the channel the link is using. */
  marginDb: number
  /** The threshold that sweep judged it against (sim 3 dB, Pluto 6 dB). */
  thresholdDb: number
  /** Iteration of the sweep, so the flag can say how old its evidence is. */
  iteration: number
  /** Offset from the carrier when a spur was localised in the signal itself, else null. */
  offsetHz: number | null
  /** "sweep" — energy detection with the link silent. "spur" — the diagnoser's read. */
  source: "sweep" | "spur"
}

export interface LinkState {
  /** Centre frequency in effect, as best the record knows it. */
  freqHz: number | null
  retunes: RetuneEvent[]
  lastRetune: RetuneEvent | null
  lastSense: SenseEvent | null
  /** Sweep evidence that the link's OWN channel is occupied, or null when there is none. */
  interferer: InterfererEvidence | null
  /** Centre frequency in effect at each iteration, for the ledger's channel column. */
  freqAt: Map<number, number>
}

const num = (v: unknown): number | null =>
  typeof v === "number" && isFinite(v) ? v : null

/** Two centre frequencies are the same channel if they are within a kilohertz. */
export const sameChannel = (a: number, b: number) => Math.abs(a - b) < 1e3

function occupancyOf(row: LedgerRow): OccupancyChannel[] {
  const raw = (row.params as { occupancy?: unknown } | undefined)?.occupancy
  if (!Array.isArray(raw)) return []
  const out: OccupancyChannel[] = []
  for (const o of raw as Record<string, unknown>[]) {
    const f = num(o?.center_freq_hz)
    const p = num(o?.power_db)
    if (f === null || p === null) continue
    out.push({
      center_freq_hz: f,
      power_db: p,
      occupied: !!o?.occupied,
      detect_margin_db: num(o?.detect_margin_db) ?? undefined,
    })
  }
  return out
}

/**
 * Replay the ledger into the link's channel history.
 *
 * Rows are walked in order, so the frequency column shows what was in effect WHEN each row was
 * written rather than what is in effect now — the whole reason a retune is legible in the history
 * at all. `snapshot.structure.center_freq_hz` wins for the live value when it is present, because
 * it is what the radio reported actually running, not what the agent asked for.
 */
export function linkState(rows: LedgerRow[], snapshot?: Snapshot | null): LinkState {
  const retunes: RetuneEvent[] = []
  const freqAt = new Map<number, number>()
  let freq: number | null = null
  let lastSense: SenseEvent | null = null

  for (const r of rows) {
    const p = (r.params ?? {}) as Record<string, unknown>
    if (r.verdict === "retuned") {
      const to = num(p.center_freq_hz)
      if (to !== null) {
        retunes.push({ iteration: r.iteration, toHz: to, fromHz: num(p.from_hz) })
        freq = to
      }
    } else if (r.verdict === "sensed") {
      const ch = occupancyOf(r)
      if (ch.length) lastSense = { iteration: r.iteration, channels: ch, atHz: num(p.center_freq_hz) ?? freq }
    } else if (num(p.center_freq_hz) !== null) {
      // A measurement stamped with its channel. Also the only source of truth for a run that
      // started on the bench's declared channel without the agent ever retuning.
      freq = num(p.center_freq_hz)
    }
    if (freq !== null) freqAt.set(r.iteration, freq)
  }

  const live = num(snapshot?.structure?.center_freq_hz) ?? freq

  // Sweep evidence for the channel the link is on. A sweep that did not cover the link's own
  // channel says nothing about it — absence of a bar is not absence of a jammer.
  let interferer: InterfererEvidence | null = null
  if (lastSense && live !== null) {
    const mine = lastSense.channels.find((c) => sameChannel(c.center_freq_hz, live))
    if (mine && mine.occupied) {
      interferer = {
        marginDb: mine.power_db,
        thresholdDb: mine.detect_margin_db ?? 3,
        iteration: lastSense.iteration,
        offsetHz: null,
        source: "sweep",
      }
    }
  }

  // Failing that, the diagnoser's spur read on the received signal itself. Weaker evidence — it
  // cannot separate an interferer from the transmitter's own LO leakage — so it is used only when
  // the sweep has nothing to say, and it is labelled as a spur rather than as a sweep.
  if (!interferer && snapshot?.diagnosis?.features) {
    const f = snapshot.diagnosis.features
    const prom = num(f.spur_prominence_db)
    const off = num(f.spur_offset_khz)
    if (prom !== null && prom > 0 && off !== null && off !== 0) {
      interferer = {
        marginDb: prom, thresholdDb: 0, iteration: -1,
        offsetHz: off * 1e3, source: "spur",
      }
    }
  }

  return {
    freqHz: live,
    retunes,
    lastRetune: retunes.length ? retunes[retunes.length - 1] : null,
    lastSense,
    interferer,
    freqAt,
  }
}
