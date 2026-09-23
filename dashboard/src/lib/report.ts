import type { DevicesPayload, ExperimentInfo, LedgerRow, Snapshot } from "@/types"

/**
 * The run report's data model: one experiment's ledger reduced to the things a reader asks about.
 *
 * This lives apart from the dialog that draws it because the reduction is the substance. A ledger
 * is a list of trials; a report is a claim about a link, and the step between them — pooling error
 * counts rather than averaging ratios, keeping short captures away from full-length ones, noticing
 * that a rung is bimodal rather than merely bad — is where a reader is misled or not.
 */

/** Bits per symbol, by every spelling the ledger uses (structure ids use the skill stem). */
const BPS: Record<string, number> = {
  bpsk: 1, qpsk: 2, "8psk": 3, psk8: 3, "16qam": 4, qam16: 4, "32qam": 5, qam32: 5,
  "64qam": 6, qam64: 6, "128qam": 7, qam128: 7, "256qam": 8, qam256: 8,
}

/** Display spelling, so a rung reads as a modulation and not as a file stem. */
const LABEL: Record<string, string> = {
  bpsk: "BPSK", qpsk: "QPSK", "8psk": "8PSK", "16qam": "16QAM", "32qam": "32QAM",
  "64qam": "64QAM", "128qam": "128QAM", "256qam": "256QAM",
}

const CANON: Record<string, string> = {
  psk8: "8psk", qam16: "16qam", qam32: "32qam", qam64: "64qam", qam128: "128qam", qam256: "256qam",
}

/** The modulation a row is about. `edit_description` is written as "run 16qam"; the structure id
 *  ("qam16_link") is the fallback, because an imported or hand-named structure may say neither. */
export function modOf(row: LedgerRow): string | null {
  const hay = `${row.edit_description ?? ""} ${row.structure_id ?? ""}`.toLowerCase()
  // Longest first, so "16qam" is not eaten by a hypothetical "6qam" and "256qam" beats "56qam".
  const keys = Object.keys(BPS).sort((a, b) => b.length - a.length)
  for (const k of keys) if (hay.includes(k)) return CANON[k] ?? k
  return null
}

export function modLabel(mod: string | null, fallback: string): string {
  if (!mod) return fallback
  return LABEL[mod] ?? mod.toUpperCase()
}

export function bpsOf(mod: string | null): number | null {
  return mod ? (BPS[mod] ?? null) : null
}

function num(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined
}

export function berOf(r: LedgerRow): number | undefined {
  const m = r.metrics ?? {}
  return num(m.BER) ?? num(m.ber)
}
export function evmOf(r: LedgerRow): number | undefined {
  const m = r.metrics ?? {}
  return num(m.EVM) ?? num(m.evm_pct)
}
export function snrOf(r: LedgerRow): number | undefined {
  const m = r.metrics ?? {}
  return num(m.SNR_est) ?? num(m.SNR) ?? num(m.snr_db)
}
function bitsOf(r: LedgerRow): number | undefined {
  return num((r.metrics ?? {}).n_bits)
}
function errorsOf(r: LedgerRow): number | undefined {
  return num((r.metrics ?? {}).n_errors)
}
function requestedOf(r: LedgerRow): number | undefined {
  return num((r.metrics ?? {}).n_bits_requested)
}

/** A trial graded every bit that was asked for. A rung whose bits/symbol does not divide the
 *  request is short by up to bps-1 bits as a matter of arithmetic, which is not a dropout — so the
 *  bar is the largest whole number of symbols that fits, not the request itself. */
function isFullLength(r: LedgerRow, bps: number | null): boolean | null {
  const n = bitsOf(r), req = requestedOf(r)
  if (n === undefined || req === undefined) return null
  const expected = bps && bps > 0 ? Math.floor(req / bps) * bps : req
  return n >= expected
}

export interface Agg {
  trials: number
  bits: number | null      // null when the ledger predates n_bits
  errors: number | null
  ber: number | null       // pooled by bits when counts exist, else the mean of the ratios
  berHi: number | null     // 95% upper bound
  pooled: boolean          // true = weighted by bits; false = mean of ratios
  evmMean: number | null
  evmSd: number | null
  evmMin: number | null
  evmMax: number | null
  snrMean: number | null
  passes: number           // trials at or under target
}

const EMPTY: Agg = {
  trials: 0, bits: null, errors: null, ber: null, berHi: null, pooled: false,
  evmMean: null, evmSd: null, evmMin: null, evmMax: null, snrMean: null, passes: 0,
}

function aggregate(rows: LedgerRow[], target: number): Agg {
  if (!rows.length) return { ...EMPTY }
  const bers = rows.map(berOf).filter((v): v is number => v !== undefined)
  const evms = rows.map(evmOf).filter((v): v is number => v !== undefined)
  const snrs = rows.map(snrOf).filter((v): v is number => v !== undefined)
  const bitsAll = rows.map(bitsOf)
  const errsAll = rows.map(errorsOf)
  const haveCounts = bitsAll.every((b) => b !== undefined) && errsAll.every((e) => e !== undefined)

  let bits: number | null = null, errors: number | null = null
  let ber: number | null = null, berHi: number | null = null
  if (haveCounts) {
    bits = bitsAll.reduce<number>((a, b) => a + (b as number), 0)
    errors = errsAll.reduce<number>((a, b) => a + (b as number), 0)
    ber = bits > 0 ? errors / bits : null
    if (bits > 0) {
      // Rule of three when nothing failed: a zero count bounds the ratio, it does not measure it.
      berHi = errors === 0 ? 3 / bits : (ber as number) + 1.96 * Math.sqrt(((ber as number) * (1 - (ber as number))) / bits)
    }
  } else if (bers.length) {
    ber = bers.reduce((a, b) => a + b, 0) / bers.length
  }

  const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null)
  const evmMean = mean(evms)
  const evmSd = evms.length > 1 && evmMean !== null
    ? Math.sqrt(evms.reduce((a, b) => a + (b - evmMean) ** 2, 0) / evms.length)
    : evms.length ? 0 : null

  return {
    trials: rows.length,
    bits, errors, ber, berHi, pooled: haveCounts,
    evmMean,
    evmSd,
    evmMin: evms.length ? Math.min(...evms) : null,
    evmMax: evms.length ? Math.max(...evms) : null,
    snrMean: mean(snrs),
    passes: bers.filter((b) => b <= target).length,
  }
}

export interface Rung {
  mod: string | null
  label: string
  bps: number | null
  structureIds: string[]
  rows: LedgerRow[]
  all: Agg
  full: Agg
  short: Agg
  hasSplit: boolean        // the ledger carried bit counts, so full/short is meaningful
  /** The verdict is taken from FULL-LENGTH trials when they exist, and from trials on the channel
   *  the run SETTLED on when it moved: a short capture is a different measurement, and so is a
   *  trial taken on a channel the run has since abandoned. Pooling a jammed trial with the one
   *  that came after the escape is how a rung that was held either side of an interference
   *  episode reads as a rung that failed. */
  verdictAgg: Agg
  /** True when the verdict rests on trials taken after the last move. False means this rung was
   *  only ever measured on a channel the run left — its verdict is inherited, not re-established,
   *  which is exactly the claim a reader must not take on trust. */
  onFinalChannel: boolean
  meets: boolean | null
  /** Trials fall into a passing cluster and a failing one rather than degrading smoothly — the
   *  signature of an acquisition that either happens or does not, as opposed to a noise limit. */
  bimodal: boolean
  bestBer: number | null
}

/** Ledger verdicts that mark a DECISION rather than a measurement. Mirrors the framework's
 *  `_DECISION_VERDICTS`: something the agent CHANGED about the link, recorded so a run reads as
 *  the choices it made and not only as the numbers those choices produced. */
const DECISION_VERDICTS = new Set(["retuned", "hopping", "hopping_cleared", "power", "sensed",
  "conclusion"])

export interface Decision {
  iteration: number
  what: string
  verdict: string
  params: Record<string, unknown>
}

export interface SensedChannel {
  center_freq_hz?: number
  power_db?: number
  occupied?: boolean
}

/** A channel that read occupied, and by how much it stood above the quietest clear candidate in
 *  the same sweep. The margin is the evidence: "occupied" is a threshold decision the backend
 *  made, whereas a channel sitting 13 dB over its neighbours is something a reader can see. */
export interface Interference {
  atHz: number | null
  powerDb: number | null
  /** dB between the occupied channel and the quietest channel that read clear. Null when the
   *  sweep found nothing clear to compare against. */
  marginDb: number | null
  /** True when the occupied channel is the one the link was sitting on when the sweep ran. */
  wasOurs: boolean
}

/** Where the link started, where it ended up, and how it got there. Null when nothing moved it —
 *  a run that never retuned has no before and after worth printing. */
export interface ChannelStory {
  fromHz: number | null
  toHz: number | null
  retunes: number
  sensed: SensedChannel[]
  /** What the last sweep FOUND, reduced to the finding rather than left as a table. A run that
   *  moved because a channel was occupied has to be able to say so in one line. */
  interference: Interference | null
  hop: { channelsHz: number[]; rateHz: number } | null
  hopCleared: boolean
  powerDb: number | null
  /** The link either side of the last change that moved it.
   *
   *  This split is not cosmetic. Pooling a jammed trial with the trial that came after the escape
   *  averages two different channels into one ratio, and the run then reads as a failure at
   *  precisely the point it succeeded. Whatever else an avoidance or hopping run reports, the
   *  number that says whether it worked is the one measured AFTER the move. */
  before: Agg | null
  after: Agg | null
  /** Iteration of the change the split is taken at. */
  pivot: number | null
}

/**
 * What the run was ABOUT, which decides what its report has to lead with.
 *
 * A ladder ends on a configuration; an avoidance run ends on a channel; a hopping run ends on a
 * rate. Printing a ladder table for a run that never climbed one, or burying a retune in a list of
 * trials, answers a question nobody asked. The kinds are ranked by how much they explain: a run
 * that hopped AND then climbed is a hopping run whose ladder is evidence, not a ladder run.
 */
export type RunKind = "hopping" | "avoidance" | "ladder" | "tuning" | "link"

function classify(rungs: Rung[], ch: ChannelStory | null, ledger: LedgerRow[]): RunKind {
  if (ch?.hop) return "hopping"
  if (ch && (ch.retunes > 0 || ch.sensed.length > 0)) return "avoidance"
  if (rungs.filter((r) => r.bps !== null).length > 1) return "ladder"
  if (ledger.some((r) => r.loop === "inner")) return "tuning"
  return "link"
}

function decisionsOf(ledger: LedgerRow[]): Decision[] {
  return ledger
    .filter((r) => DECISION_VERDICTS.has(r.verdict))
    .map((r) => ({
      iteration: r.iteration,
      what: r.edit_description,
      verdict: r.verdict,
      params: (r.params ?? {}) as Record<string, unknown>,
    }))
}

/** The iteration of the last change that MOVED the link, or null if nothing did. Computed on its
 *  own because the rungs need it before the channel story can be built: a rung's verdict depends
 *  on which side of the move its trials fall, and the channel story's "after" depends on which
 *  rung the run settled on. */
export function pivotOf(decisions: Decision[]): number | null {
  const moves = decisions
    .filter((d) => d.verdict === "retuned" || d.verdict === "hopping")
    .sort((a, b) => a.iteration - b.iteration)
  return moves.length ? moves[moves.length - 1].iteration : null
}

/** The occupied channel from a sweep, with the margin that makes it visible rather than asserted. */
function interferenceOf(sensed: SensedChannel[], atHz: number | null): Interference | null {
  const busy = sensed.filter((c) => c.occupied && typeof c.power_db === "number")
  if (!busy.length) return null
  // The loudest, when more than one reads occupied: it is the one a reader will ask about.
  const worst = busy.reduce((a, b) => ((b.power_db as number) > (a.power_db as number) ? b : a))
  const clear = sensed
    .filter((c) => !c.occupied && typeof c.power_db === "number")
    .map((c) => c.power_db as number)
  return {
    atHz: worst.center_freq_hz ?? null,
    powerDb: worst.power_db ?? null,
    marginDb: clear.length ? (worst.power_db as number) - Math.min(...clear) : null,
    wasOurs: atHz !== null && worst.center_freq_hz !== undefined
      && Math.abs(worst.center_freq_hz - atHz) < 1e3,
  }
}

function channelStory(decisions: Decision[], graded: LedgerRow[], target: number,
                      pivot: number | null, settled: Rung | null): ChannelStory | null {
  const retunes = decisions.filter((d) => d.verdict === "retuned")
  const sensedRows = decisions.filter((d) => d.verdict === "sensed")
  const hops = decisions.filter((d) => d.verdict === "hopping")
  const cleared = decisions.some((d) => d.verdict === "hopping_cleared")
  const power = decisions.filter((d) => d.verdict === "power")
  if (!retunes.length && !sensedRows.length && !hops.length && !power.length) return null

  const n = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null)
  // The first retune knows where it came FROM; without one there is no before to report.
  const fromHz = retunes.length ? n(retunes[0].params.from_hz) : null
  const toHz = retunes.length ? n(retunes[retunes.length - 1].params.center_freq_hz) : null
  const lastHop = hops.length ? hops[hops.length - 1] : null
  const lastSweep = sensedRows.length ? sensedRows[sensedRows.length - 1] : null
  const sensed = lastSweep ? ((lastSweep.params.occupancy as SensedChannel[]) ?? []) : []
  // The sweep is judged against the channel the link was ON when it ran, which the framework
  // stamps on the row. Without it "occupied" is a fact about the band and not about this link.
  const interference = interferenceOf(sensed, lastSweep ? n(lastSweep.params.center_freq_hz) : null)

  // Split the trials at the LAST change that moved the link. Everything before it measured a
  // channel the run has since left; everything after it measures the one it settled on.
  //
  // "After" is narrowed further, to the rung the run SETTLED on. The post-move trials are not one
  // measurement: a ladder that re-probes the rung above after moving records that probe's failure
  // here too, and a handful of errors from the rung being held is then swamped by tens of
  // thousands from a rung nobody claimed. The question this number answers is "did the link come
  // back", so it is the held rung's ratio, not every bit sent after the retune.
  //
  // Both sides are scoped to the rung the run SETTLED on, and on the "after" side to full-length
  // trials, so the pair reads as one rung measured twice rather than as two piles of mixed
  // modulations. Each narrowing falls back to the wider set when it would otherwise be empty: a
  // run that never held anything still gets a before and an after, just a blunter one.
  const narrow = (side: (it: number) => boolean, fullOnly: boolean): LedgerRow[] | null => {
    if (pivot === null) return null
    const wide = graded.filter((r) => side(r.iteration))
    if (!settled) return wide
    const mine = settled.rows.filter((r) => side(r.iteration))
    if (!mine.length) return wide
    const full = fullOnly ? mine.filter((r) => isFullLength(r, settled.bps) === true) : []
    return full.length ? full : mine
  }
  const before = narrow((it) => it < pivot!, false)
  const after = narrow((it) => it > pivot!, true)

  return {
    fromHz,
    toHz,
    retunes: retunes.length,
    sensed,
    interference,
    hop: lastHop
      ? {
          channelsHz: (lastHop.params.channels_hz as number[]) ?? [],
          rateHz: n(lastHop.params.hop_rate_hz) ?? 0,
        }
      : null,
    hopCleared: cleared,
    powerDb: power.length ? n(power[power.length - 1].params.tx_power_db) : null,
    before: before && before.length ? aggregate(before, target) : null,
    after: after && after.length ? aggregate(after, target) : null,
    pivot,
  }
}

export interface RunReport {
  /** Stable identity, so one completed run yields exactly one popup. */
  id: string
  experimentName: string
  slug: string | null
  goal: string
  backend: string
  isHardware: boolean
  iterations: number
  trials: number
  startedAt: number | null
  endedAt: number | null
  targetBer: number
  rungs: Rung[]
  /** What the run was about, and therefore what its report leads with. */
  kind: RunKind
  /** Everything the agent CHANGED, in order. Empty for a run that only measured. */
  decisions: Decision[]
  /** Before/after for the link's channel, when something moved it. */
  channel: ChannelStory | null
  /** The agent's own closing line, if it gave one to finish_experiment. */
  conclusion: string | null
  settled: Rung | null      // highest bits/symbol that met the target
  blocked: Rung | null      // the next rung up that did not
  dropouts: { short: number; total: number } | null
  diagnosis: Snapshot["diagnosis"] | null
  snapshot: Snapshot | null
  finalStructureId: string | null
  ledger: LedgerRow[]
}

export function buildReport(
  ledger: LedgerRow[],
  snapshot: Snapshot | null,
  experiment: ExperimentInfo | null,
  devices: DevicesPayload | null,
): RunReport | null {
  const runs = ledger.filter((r) => berOf(r) !== undefined)
  // A run with no graded trial at all is still a run: a band sweep that found nowhere to go has
  // decisions worth reporting and no measurements. Only a wholly empty ledger has nothing to say.
  if (!runs.length && !ledger.some((r) => DECISION_VERDICTS.has(r.verdict))) return null

  const target = snapshot?.metrics?.target_ber ?? 1e-2

  // Group by modulation, falling back to the structure id so a run with an unrecognised name
  // still appears rather than being silently dropped from its own report.
  const groups = new Map<string, LedgerRow[]>()
  for (const r of runs) {
    const key = modOf(r) ?? r.structure_id ?? "unknown"
    const list = groups.get(key)
    if (list) list.push(r)
    else groups.set(key, [r])
  }

  // Where the link last moved. Rungs are judged on the far side of it, so this has to be known
  // before any of them are built.
  const decisions = decisionsOf(ledger)
  const pivot = pivotOf(decisions)

  const rungs: Rung[] = [...groups.entries()].map(([key, rows]) => {
    const mod = modOf(rows[0])
    const bps = bpsOf(mod)
    // Two independent reasons a trial must not decide a rung's verdict: it graded fewer bits than
    // were asked for, or it was taken on a channel the run has since left. Apply the channel
    // filter first, because a rung re-established after a move is answering a different question
    // from the one it answered before the move.
    const onFinal = pivot === null ? rows : rows.filter((r) => r.iteration > pivot)
    const judged = onFinal.length ? onFinal : rows
    const onFinalChannel = pivot === null || onFinal.length > 0
    const full = judged.filter((r) => isFullLength(r, bps) === true)
    const short = judged.filter((r) => isFullLength(r, bps) === false)
    const hasSplit = full.length + short.length === judged.length && judged.length > 0
    const all = aggregate(judged, target)
    const fullAgg = aggregate(full, target)
    const shortAgg = aggregate(short, target)
    const verdictAgg = hasSplit && full.length > 0 ? fullAgg : all
    const bers = judged.map(berOf).filter((v): v is number => v !== undefined)
    const passing = bers.filter((b) => b <= target).length
    return {
      mod,
      label: modLabel(mod, key),
      bps,
      structureIds: [...new Set(rows.map((r) => r.structure_id).filter(Boolean))],
      // Every trial of this rung, both sides of the move: the verdict narrows, the evidence does
      // not, so the trial table still shows what the rung did on the channel the run escaped.
      rows,
      all,
      full: fullAgg,
      short: shortAgg,
      hasSplit,
      verdictAgg,
      onFinalChannel,
      meets: verdictAgg.ber === null ? null : verdictAgg.ber <= target,
      // Both clusters present and populated: some trials comfortably meet the target while
      // others miss it by more than an order of magnitude.
      bimodal: passing > 0 && bers.some((b) => b > Math.max(target * 10, 0.05)),
      bestBer: bers.length ? Math.min(...bers) : null,
    }
  })

  // The ladder reads bottom-up by bits/symbol; anything unrecognised sorts last by name.
  rungs.sort((a, b) => {
    if (a.bps !== null && b.bps !== null) return a.bps - b.bps
    if (a.bps !== null) return -1
    if (b.bps !== null) return 1
    return a.label.localeCompare(b.label)
  })

  const passing = rungs.filter((r) => r.meets === true && r.bps !== null)
  const settled = passing.length ? passing[passing.length - 1] : null
  const blocked = settled
    ? rungs.find((r) => r.bps !== null && settled.bps !== null && r.bps > settled.bps) ?? null
    : rungs.find((r) => r.meets === false) ?? null

  const shortCount = rungs.reduce((a, r) => a + (r.hasSplit ? r.short.trials : 0), 0)
  const splitKnown = rungs.some((r) => r.hasSplit)

  const last = runs.length ? runs[runs.length - 1] : ledger[ledger.length - 1]
  const backend = experiment?.backend || devices?.backend || ""
  const channel = channelStory(decisions, runs, target, pivot, settled)
  const closing = [...decisions].reverse().find((d) => d.verdict === "conclusion") ?? null

  return {
    id: `${experiment?.slug ?? "run"}:${last.iteration}:${runs.length}`,
    experimentName: experiment?.name ?? "Untitled run",
    slug: experiment?.slug ?? null,
    goal: experiment?.goal ?? snapshot?.goal ?? "",
    backend,
    isHardware: /pluto|radio|hackrf|usrp|iio/i.test(backend),
    iterations: experiment?.iterations ?? ledger.length,
    trials: runs.length,
    startedAt: experiment?.started_at ?? null,
    endedAt: experiment?.last_activity ?? experiment?.ended_at ?? null,
    targetBer: target,
    rungs,
    kind: classify(rungs, channel, ledger),
    decisions,
    channel,
    conclusion: closing ? closing.what : null,
    settled,
    blocked,
    dropouts: splitKnown ? { short: shortCount, total: runs.length } : null,
    diagnosis: snapshot?.diagnosis ?? null,
    snapshot,
    finalStructureId: last.structure_id ?? null,
    ledger,
  }
}

// ---- the outcome -----------------------------------------------------------------------------

/**
 * What the whole run came to, in the shape its kind calls for.
 *
 * One place, used by the popup and by the Markdown it exports, so the two cannot drift. The rule
 * is the same whatever the run did: lead with the OUTCOME over the entire flow — the configuration
 * settled on, the channel ended up on, the rate that evaded — and leave the steps to be evidence
 * for it. A report that opens with a log of what happened makes the reader do the reduction the
 * report exists to have done.
 */
export interface Outcome {
  headline: string
  stats: { label: string; value: string; tone?: "good" | "bad" | "warn" }[]
}

export function fmtHz(hz: number | null | undefined): string {
  if (hz == null || !Number.isFinite(hz)) return "—"
  if (Math.abs(hz) >= 1e9) return `${(hz / 1e9).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")} GHz`
  if (Math.abs(hz) >= 1e6) return `${(hz / 1e6).toFixed(3).replace(/0+$/, "").replace(/\.$/, "")} MHz`
  if (Math.abs(hz) >= 1e3) return `${(hz / 1e3).toFixed(1)} kHz`
  return `${hz.toFixed(0)} Hz`
}

export function outcomeOf(rep: RunReport): Outcome {
  const t = rep.targetBer.toExponential(0)
  const settled = rep.settled
  const ber = (r: Rung) => fmtBer(r.verdictAgg.ber, r.verdictAgg.berHi, r.verdictAgg.errors)
  const ch = rep.channel

  // The configuration the run kept, which every kind reports even when it is not the headline:
  // whatever else happened, this is what the link is running now. `modLabel` rather than the raw
  // structure id, so a fallback reads as a modulation and not as a file stem.
  const finalMod = rep.ledger.length ? modOf(rep.ledger[rep.ledger.length - 1]) : null
  const kept: Outcome["stats"][number] = {
    label: "Settled on",
    value: settled ? settled.label : finalMod ? modLabel(finalMod, "none") : "none",
    tone: settled ? "good" : "bad",
  }

  // For a run that MOVED the link, the number that says whether it worked is the one measured
  // afterwards. Pooling across the move averages the channel the run escaped with the one it
  // escaped to, and the run then reads as a failure at exactly the point it succeeded.
  const aggBer = (a: Agg | null) => (a ? fmtBer(a.ber, a.berHi, a.errors) : "—")
  const worked = ch?.after ? ch.after.ber !== null && ch.after.ber <= rep.targetBer : settled !== null

  if (rep.kind === "hopping" && ch?.hop) {
    return {
      headline:
        `Hopping ${ch.hop.channelsHz.length} channels at ${fmtHz(ch.hop.rateHz)}` +
        (worked
          ? `, and the link held ${settled?.label ?? "the structure"} at ${aggBer(ch.after) !== "—" ? aggBer(ch.after) : settled ? ber(settled) : "target"}.`
          : `, and the link still did not meet ${t}.`) +
        (ch.before ? ` Before the plan it was ${aggBer(ch.before)}.` : "") +
        (ch.hopCleared ? " The plan was cleared before the run ended." : ""),
      stats: [
        { label: "Hop rate", value: fmtHz(ch.hop.rateHz), tone: worked ? "good" : "bad" },
        { label: "Before", value: aggBer(ch.before), tone: "bad" },
        { label: "After", value: aggBer(ch.after), tone: worked ? "good" : "bad" },
        { label: `Target ${t}`, value: worked ? "met" : "not met", tone: worked ? "good" : "bad" },
      ],
    }
  }

  if (rep.kind === "avoidance" && ch) {
    const moved = ch.fromHz !== null && ch.toHz !== null && ch.fromHz !== ch.toHz
    const clear = ch.sensed.filter((c) => !c.occupied).length
    const jam = ch.interference
    // The finding, not the table. A run that moved because a channel was occupied has to say so
    // in the line a reader reads first, with the margin that makes it evidence rather than a
    // claim: a sweep row buried under "Decisions" is why an interference episode could be the
    // whole story of a run and still be absent from its own report.
    const found = jam
      ? `Found ${jam.wasOurs ? "the link's own channel" : fmtHz(jam.atHz)} occupied` +
        (jam.marginDb !== null ? `, ${jam.marginDb.toFixed(1)} dB over the quietest candidate` : "") +
        (jam.wasOurs && jam.atHz !== null ? ` at ${fmtHz(jam.atHz)}` : "") + ". "
      : ""
    return {
      headline:
        found +
        (moved
          ? `Moved the link from ${fmtHz(ch.fromHz)} to ${fmtHz(ch.toHz)}`
          : ch.toHz !== null
            ? `Held the link at ${fmtHz(ch.toHz)}`
            : "Swept the band") +
        (ch.sensed.length ? ` after sensing ${ch.sensed.length} candidates (${clear} clear)` : "") +
        (ch.after
          ? `, and ${settled ? settled.label : "the link"} went from ${aggBer(ch.before)} to ${aggBer(ch.after)}` +
            (worked ? `, inside the ${t} target.` : `, still outside the ${t} target.`)
          : worked
            ? `, and it carried ${settled?.label ?? "the link"} at ${settled ? ber(settled) : "target"}.`
            : `, and nothing met ${t}.`),
      stats: [
        { label: "Channel", value: `${fmtHz(ch.fromHz)} → ${fmtHz(ch.toHz)}`, tone: moved ? "good" : undefined },
        jam
          ? {
              label: "Occupied",
              value: `${fmtHz(jam.atHz)}${jam.marginDb !== null ? ` · +${jam.marginDb.toFixed(1)} dB` : ""}`,
              tone: "bad" as const,
            }
          : { label: "Before", value: aggBer(ch.before), tone: ch.before ? ("bad" as const) : undefined },
        { label: "After", value: aggBer(ch.after), tone: worked ? "good" : "bad" },
        { label: `Target ${t}`, value: worked ? "met" : "not met", tone: worked ? "good" : "bad" },
      ],
    }
  }

  if (rep.kind === "tuning") {
    const inner = rep.ledger.filter((r) => r.loop === "inner").length
    return {
      headline: settled
        ? `Tuned ${settled.label} over ${inner} inner-loop trials and kept it at ${ber(settled)}.`
        : `Tuned over ${inner} inner-loop trials without meeting ${t}.`,
      stats: [
        kept,
        { label: "Inner-loop trials", value: String(inner) },
        { label: "Error ratio", value: settled ? ber(settled) : "—", tone: settled ? "good" : undefined },
        { label: `Target ${t}`, value: settled ? "met" : "not met", tone: settled ? "good" : "bad" },
      ],
    }
  }

  // Ladder (and the single-structure link run, which is a ladder of one).
  const blocked = rep.blocked
  // A rung blocked on a channel the run has since left was never re-established on the one it
  // settled on, so its failure is not evidence about the link as it now stands. Saying "16-QAM
  // did not hold" without that qualifier hands the reader an interferer's verdict as if it were
  // the radio's.
  const staleBlock = blocked !== null && !blocked.onFinalChannel
  return {
    headline: settled
      ? `Settled on ${settled.label}${settled.bps ? ` at ${settled.bps} bits/symbol` : ""}, ` +
        `error ratio ${ber(settled)} inside the ${t} target` +
        (blocked
          ? staleBlock
            ? `; ${blocked.label} did not hold, but was last measured before the link moved.`
            : `; ${blocked.label} did not hold.`
          : ".")
      : `No structure met the ${t} target.`,
    stats: [
      kept,
      { label: "Spectral efficiency", value: settled?.bps ? `${settled.bps} b/sym` : "—" },
      { label: "Error ratio", value: settled ? ber(settled) : "—", tone: settled ? "good" : undefined },
      {
        label: `Target ${t}`,
        value: blocked
          ? `${blocked.label} failed${staleBlock ? " (pre-move)" : ""}`
          : settled ? "met" : "not met",
        tone: blocked ? (staleBlock ? "warn" : "bad") : settled ? "good" : "warn",
      },
    ],
  }
}

// ---- formatting ------------------------------------------------------------------------------

export function fmtBer(v: number | null, hi?: number | null, errors?: number | null): string {
  if (v === null) return "—"
  if (errors === 0 && hi != null) return `<${hi.toExponential(1)}`
  if (v === 0) return "0"
  return v.toExponential(2)
}

export function fmtInt(v: number | null | undefined): string {
  return v == null ? "—" : v.toLocaleString("en-US")
}

/** The run's date in the reader's own timezone. `toISOString` would render a late-evening bench
 *  session as the following day, which is the one date a reader would notice being wrong. */
export function fmtDate(epochS: number | null): string {
  const d = epochS ? new Date(epochS * 1000) : new Date()
  const p = (n: number) => String(n).padStart(2, "0")
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

export function fmtDuration(fromS: number | null, toS: number | null): string {
  if (!fromS || !toS || toS < fromS) return "—"
  const s = Math.round(toS - fromS)
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
  if (h) return `${h}h ${m}m`
  if (m) return `${m}m ${s % 60}s`
  return `${s}s`
}

/** The report as Markdown, for the downloadables shelf. Same numbers as the page, one file. */
export function toMarkdown(rep: RunReport): string {
  const L: string[] = []
  const date = fmtDate(rep.endedAt)
  const meansOnly = rep.rungs.some((r) => !r.verdictAgg.pooled)
  L.push(`# ${rep.experimentName} — run report`, "")
  L.push(`**Backend:** ${rep.backend || "unknown"} (${rep.isHardware ? "real radios" : "model / simulation"})  `)
  L.push(`**Date:** ${date}  `)
  L.push(`**Trials:** ${rep.trials} over ${rep.rungs.length} structure${rep.rungs.length === 1 ? "" : "s"}  `)
  L.push(`**BER target:** ${rep.targetBer.toExponential(0)}`, "")
  if (meansOnly) {
    L.push("", "> Error ratios below are the MEAN of the per-trial ratios. This ledger carries no " +
      "per-trial bit counts, so they cannot be weighted by bits, and full-length trials cannot be " +
      "separated from short captures.")
  }
  if (rep.goal) L.push(`**Goal:** ${rep.goal}`, "")

  L.push("## Outcome", "")
  L.push(outcomeOf(rep).headline)
  if (rep.conclusion) L.push("", `> ${rep.conclusion}`)
  L.push("")
  if (rep.channel) {
    const c = rep.channel
    L.push("### Channel", "")
    if (c.fromHz !== null || c.toHz !== null) {
      L.push(`**Channel:** ${fmtHz(c.fromHz)} → ${fmtHz(c.toHz)} · ` +
        `${c.retunes} retune${c.retunes === 1 ? "" : "s"}`)
    }
    if (c.before || c.after) {
      L.push("", `**Link before the move:** ${c.before ? fmtBer(c.before.ber, c.before.berHi, c.before.errors) : "—"}` +
        `${c.before ? ` over ${c.before.trials} trial${c.before.trials === 1 ? "" : "s"}` : ""} · ` +
        `**after:** ${c.after ? fmtBer(c.after.ber, c.after.berHi, c.after.errors) : "—"}` +
        `${c.after ? ` over ${c.after.trials} trial${c.after.trials === 1 ? "" : "s"}` : ""}`,
        "", "Measured either side of the change; the two are not pooled, because they are two " +
        "different channels.")
    }
    if (c.hop) {
      L.push(`**Hop plan:** ${c.hop.channelsHz.length} channels at ${fmtHz(c.hop.rateHz)}` +
        (c.hopCleared ? " (cleared before the end)" : ""))
    }
    if (c.powerDb !== null) L.push(`**TX power:** ${c.powerDb >= 0 ? "+" : ""}${c.powerDb.toFixed(1)} dB`)
    if (c.sensed.length) {
      L.push("", "| Candidate | Power over noise | Reading |", "|---|---|---|")
      for (const o of c.sensed) {
        L.push(`| ${fmtHz(o.center_freq_hz ?? null)} | ` +
          `${typeof o.power_db === "number" ? `${o.power_db.toFixed(1)} dB` : "—"} | ` +
          `${o.occupied ? "occupied" : "clear"} |`)
      }
    }
    L.push("")
  }
  if (rep.decisions.length) {
    L.push("### Decisions", "")
    for (const d of rep.decisions) {
      if (d.verdict === "conclusion") continue
      L.push(`- \`#${d.iteration}\` ${d.what}`)
    }
    L.push("")
  }
  // A run that moved the link measured more than one channel, and the per-structure pool below
  // spans both. Saying a structure "did not hold" off that pool would contradict the before/after
  // above, which is the number that actually answers the question.
  const acrossChannels = rep.channel?.pivot != null
  if (acrossChannels) {
    L.push("The per-structure figures below pool trials from BOTH channels and are not the verdict " +
      "on this run; the before/after above is.")
  } else if (rep.settled) {
    L.push(`Kept **${rep.settled.label}**${rep.settled.bps ? ` (${rep.settled.bps} bits/symbol)` : ""}, ` +
      `pooled BER ${fmtBer(rep.settled.verdictAgg.ber, rep.settled.verdictAgg.berHi, rep.settled.verdictAgg.errors)} ` +
      `over ${rep.settled.verdictAgg.trials} trial${rep.settled.verdictAgg.trials === 1 ? "" : "s"}.`)
  } else if (rep.trials) {
    L.push("No structure met the BER target.")
  }
  if (rep.blocked && !acrossChannels) {
    L.push("", `**${rep.blocked.label}** did not hold: pooled BER ` +
      `${fmtBer(rep.blocked.verdictAgg.ber, rep.blocked.verdictAgg.berHi, rep.blocked.verdictAgg.errors)}` +
      (rep.blocked.bimodal
        ? `, and it is bimodal — ${rep.blocked.verdictAgg.passes} of ${rep.blocked.verdictAgg.trials} full-length trials met the target while the rest failed by more than an order of magnitude. That is an acquisition that either happens or does not, not a noise limit.`
        : "."))
  }
  L.push("")

  L.push(rep.rungs.length > 1 ? "## Ladder" : "## Structure", "")
  const split = rep.rungs.some((r) => r.hasSplit)
  L.push(split
    ? "| Rung | b/sym | Full-length | Pass | Pooled BER | EVM mean ± sd | EVM range | Short |"
    : "| Rung | b/sym | Trials | Pass | BER | EVM mean ± sd | EVM range |")
  L.push(split ? "|---|---|---|---|---|---|---|---|" : "|---|---|---|---|---|---|---|")
  for (const r of rep.rungs) {
    const a = r.verdictAgg
    const evm = a.evmMean === null ? "—" : `${a.evmMean.toFixed(2)} ± ${(a.evmSd ?? 0).toFixed(2)}%`
    const range = a.evmMin === null ? "—" : `${a.evmMin.toFixed(2)}–${(a.evmMax ?? 0).toFixed(2)}`
    const cells = [
      // A dagger marks a rung whose verdict rests on trials from a channel the run has left.
      r.onFinalChannel ? r.label : `${r.label} †`,
      r.bps ?? "—", a.trials, `${a.passes}/${a.trials}`,
      fmtBer(a.ber, a.berHi, a.errors), evm, range,
    ]
    if (split) cells.push(String(r.short.trials))
    L.push(`| ${cells.join(" | ")} |`)
  }
  L.push("")

  if (rep.rungs.some((r) => !r.onFinalChannel)) {
    L.push("## Rungs not re-established after the move", "",
      "† marks a rung last measured on a channel this run has since left. Its verdict above is " +
      "inherited from that channel, not re-established on the one the run settled on — so it " +
      "says what the rung did under the old conditions, and nothing about what it would do now.",
      "")
  }

  if (rep.dropouts && rep.dropouts.short > 0) {
    L.push("## Capture integrity", "",
      `${rep.dropouts.short} of ${rep.dropouts.total} trials returned a short capture and are ` +
      "reported separately. A trial that graded fewer bits than were asked for is not the same " +
      "measurement as one that graded them all, so short captures are excluded from the pooled " +
      "ratios above rather than averaged into them.", "")
  }

  if (rep.diagnosis) {
    L.push("## Diagnosis at the final state", "",
      `\`${rep.diagnosis.fault}\` (confidence ${rep.diagnosis.confidence.toFixed(2)}) — ${rep.diagnosis.summary}`, "",
      `Recommended action: ${rep.diagnosis.action}`, "")
  }

  L.push("## Trials", "")
  L.push("| # | Structure | BER | EVM % | SNR dB | Bits | Errors |")
  L.push("|---|---|---|---|---|---|---|")
  for (const r of rep.ledger.filter((x) => berOf(x) !== undefined)) {
    L.push(`| ${r.iteration} | ${r.structure_id} | ${(berOf(r) as number).toExponential(2)} | ` +
      `${evmOf(r)?.toFixed(2) ?? "—"} | ${snrOf(r)?.toFixed(2) ?? "—"} | ` +
      `${fmtInt(num((r.metrics ?? {}).n_bits))} | ${fmtInt(num((r.metrics ?? {}).n_errors))} |`)
  }
  L.push("")
  return L.join("\n")
}

/** The ledger as CSV, for the downloadables shelf. */
export function toCsv(rep: RunReport): string {
  const head = ["iteration", "loop", "structure_id", "edit_description", "modulation",
    "bits_per_symbol", "ber", "evm_pct", "snr_db", "n_bits", "n_errors", "verdict"]
  const lines = [head.join(",")]
  for (const r of rep.ledger) {
    const mod = modOf(r)
    const cell = (v: unknown) => {
      const s = v == null ? "" : String(v)
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
    }
    lines.push([
      r.iteration, r.loop, r.structure_id, r.edit_description, mod ?? "",
      bpsOf(mod) ?? "", berOf(r) ?? "", evmOf(r) ?? "", snrOf(r) ?? "",
      num((r.metrics ?? {}).n_bits) ?? "", num((r.metrics ?? {}).n_errors) ?? "", r.verdict,
    ].map(cell).join(","))
  }
  return lines.join("\n")
}
