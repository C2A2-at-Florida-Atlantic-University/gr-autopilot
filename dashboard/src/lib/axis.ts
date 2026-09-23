/**
 * Shared axis helpers.
 *
 * Every plot on this page had its own ad-hoc tick logic and its own idea of what "0" meant. A
 * frequency axis that reads -0.5 .. 0 .. +0.5 is not a measurement anyone can act on: the reader
 * has to know the span, the centre and the units before the picture means anything. These helpers
 * exist so a tick is always a real quantity with a unit attached.
 */

const UNITS: [number, string][] = [[1e9, "GHz"], [1e6, "MHz"], [1e3, "kHz"], [1, "Hz"]]

function unitFor(mag: number): [number, string] {
  for (const u of UNITS) if (Math.abs(mag) >= u[0]) return u
  return [1, "Hz"]
}

/** Engineering-notation frequency, chosen so 2.37 GHz reads as "2.370 GHz" not "2.37e9". */
export function fmtFreq(hz: number, digits = 3): string {
  const [scale, suffix] = unitFor(hz)
  return `${(hz / scale).toFixed(digits)} ${suffix}`
}

/**
 * A centre frequency, in the form an instrument states one.
 *
 * `fmtFreq` rounds to three decimals in whatever unit the magnitude picks, which silently throws
 * away everything below a megahertz once the value is in gigahertz: a link at 2320.500 MHz and one
 * at 2320.000 MHz both print as "2.320 GHz". For the one number the whole axis is measured
 * against, that is not good enough. Megahertz at three decimals is the convention every spectrum
 * analyser uses, it resolves to a kilohertz, and it keeps a constant width as the link retunes.
 */
export function fmtCentre(hz: number): string {
  const a = Math.abs(hz)
  if (a >= 1e6) return `${(hz / 1e6).toFixed(3)} MHz`
  if (a >= 1e3) return `${(hz / 1e3).toFixed(3)} kHz`
  return `${hz.toFixed(1)} Hz`
}

/**
 * The same number as `fmtCentre`, with trailing zeros dropped.
 *
 * `fmtCentre` pads to three decimals so a column of centre frequencies keeps a constant width.
 * On a plot axis the opposite is wanted: the centre tick sits between the +/-100 kHz ticks and
 * has to earn its width, because a label that is wide enough pushes its neighbours off the axis
 * entirely. "2370 MHz" is the same measurement as "2370.000 MHz" in a third of the space, and a
 * link parked on an odd channel still prints every digit that matters ("2369.65 MHz").
 */
export function fmtCentreCompact(hz: number): string {
  const a = Math.abs(hz)
  const [scale, suffix] = a >= 1e6 ? [1e6, "MHz"] : a >= 1e3 ? [1e3, "kHz"] : [1, "Hz"]
  const v = hz / scale
  // Three decimals resolves a kilohertz at megahertz scale, which is the finest the radios tune.
  return `${parseFloat(v.toFixed(3))} ${suffix}`
}

/** Offset from a centre, always signed, so a reader can tell which side of the carrier they are on. */
export function fmtOffset(hz: number): string {
  const s = hz >= 0 ? "+" : "-"
  const a = Math.abs(hz)
  if (a >= 1e6) return `${s}${(a / 1e6).toFixed(2)} MHz`
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(0)} kHz`
  return `${s}${a.toFixed(0)} Hz`
}

/** "1.2e-3"-style label for a log axis, kept short enough for a 9px tick. */
export function fmtSci(v: number): string {
  if (v === 0) return "0"
  const e = Math.floor(Math.log10(v))
  return `1e${e}`
}

/** Round, human tick values covering [lo, hi] — the 1/2/5 sequence every instrument uses. */
export function niceTicks(lo: number, hi: number, target = 5): number[] {
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo) return [lo]
  const raw = (hi - lo) / Math.max(1, target)
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const norm = raw / mag
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag
  const out: number[] = []
  for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) out.push(t)
  return out
}

/** Decade ticks for a log axis. */
export function decadeTicks(lo: number, hi: number): number[] {
  const out: number[] = []
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) out.push(Math.pow(10, e))
  return out.filter((v) => v >= lo * 0.999 && v <= hi * 1.001)
}

// ---- frequency axes ---------------------------------------------------------------------------

export interface FreqTick {
  /** Absolute frequency, so a caller can position it against an absolute span. */
  hz: number
  label: string
  /** The carrier itself. Kept first when labels have to be thinned, and worth a marker line. */
  centre: boolean
}

export interface FreqAxis {
  ticks: FreqTick[]
  /** What the tick labels factored out — the centre, when the ticks are offsets. Draw it once. */
  caption: string | null
  mode: "absolute" | "offset"
}

/** Decimal places that make one `step` a VISIBLE change at this scale. */
function decimalsFor(step: number, scale: number): number {
  const s = Math.abs(step) / scale
  if (!(s > 0) || !isFinite(s)) return 0
  return Math.max(0, Math.min(6, Math.ceil(-Math.log10(s) - 1e-9)))
}

/** Widen the precision until no two labels collapse onto the same text. */
function distinctLabels(values: number[], scale: number, start: number,
                        render: (v: number, d: number) => string): string[] {
  for (let d = start; d <= 6; d++) {
    const out = values.map((v) => render(v / scale, d))
    if (new Set(out).size === out.length) return out
  }
  return values.map((v) => render(v / scale, 6))
}

/**
 * Ticks for a frequency axis that a reader can actually use.
 *
 * The failure this exists to prevent: a 521 kHz span on a 2.320 GHz carrier, labelled absolutely
 * in gigahertz at three decimals, prints "2.320 GHz" under every single tick -- five identical
 * labels, one of which (the one that happened to land on a whole megahertz) reads "2 GHz" because
 * the old code dropped to zero decimals there. The axis said nothing at all.
 *
 * The fix is the convention every spectrum analyser already uses. When the span is a small
 * fraction of the centre, the leading digits are the same on every tick and carry no information,
 * so the ticks either side of the carrier become signed offsets and the CENTRE tick carries the
 * absolute frequency they are measured from -- the axis states its own reference instead of
 * relying on a caption somewhere else. When the span is wide enough for absolute values to differ
 * usefully, every tick stays absolute -- and the precision is derived from the TICK STEP rather
 * than a fixed three decimals, so adjacent ticks can never print the same text.
 */
export function freqAxis(centreHz: number, spanHz: number, target = 5): FreqAxis {
  if (!isFinite(centreHz) || !isFinite(spanHz) || spanHz <= 0) {
    return { ticks: [], caption: null, mode: "absolute" }
  }
  const lo = centreHz - spanHz / 2
  const hi = centreHz + spanHz / 2
  const values = niceTicks(lo, hi, target)
  const step = values.length > 1 ? values[1] - values[0] : spanHz

  // A span under 1% of the centre means every absolute tick repeats the same leading digits.
  const narrow = Math.abs(centreHz) > 0 && spanHz < Math.abs(centreHz) / 100

  if (narrow) {
    const [scale, suffix] = unitFor(Math.max(Math.abs(step), spanHz / 4))
    const d = decimalsFor(step, scale)
    const offsets = values.map((v) => v - centreHz)

    // The carrier's own tick is labelled with the FREQUENCY, not with "0".
    //
    // "0" is only legible to a reader who already knows what the axis is centred on, which is
    // exactly the thing a frequency axis should be telling them. A caption in the panel title
    // ("Spectrum - PSD (dB) - centred 2370.000 MHz") would put the one number the whole axis is
    // measured against furthest from the axis, and leave the plot meaningless the moment it is
    // cropped, screenshotted or printed. Putting it on the
    // centre tick costs nothing: the offsets either side stay offsets, and the reader can add.
    let cIdx = 0
    for (let i = 1; i < offsets.length; i++) {
      if (Math.abs(offsets[i]) < Math.abs(offsets[cIdx])) cIdx = i
    }
    const centreTick = Math.abs(offsets[cIdx]) <= Math.abs(step) / 2 ? cIdx : -1

    const labels = distinctLabels(offsets, scale, d, (v, dd) =>
      `${v > 0 ? "+" : "-"}${Math.abs(v).toFixed(dd)} ${suffix}`)
    return {
      ticks: values.map((hz, i) => ({
        hz,
        label: i === centreTick ? fmtCentreCompact(centreHz) : labels[i],
        centre: i === centreTick,
      })),
      caption: null,
      mode: "offset",
    }
  }

  const [scale, suffix] = unitFor(Math.max(Math.abs(hi), Math.abs(lo)))
  const d = decimalsFor(step, scale)
  const labels = distinctLabels(values, scale, d, (v, dd) => `${v.toFixed(dd)} ${suffix}`)
  return {
    ticks: values.map((hz, i) => ({
      hz, label: labels[i], centre: Math.abs(hz - centreHz) < Math.abs(step) / 2,
    })),
    caption: null,
    mode: "absolute",
  }
}

/**
 * Drop labels that would touch.
 *
 * Canvas will happily draw text on top of text, which is how a tick row turns into a smear and how
 * the peak cursor ends up written through the interferer flag. Items are placed in priority order
 * and anything that would overlap something already placed -- or a reserved region such as a
 * corner caption -- is dropped rather than drawn illegibly. The survivors come back in x order,
 * ready to draw.
 */
export function placeLabels<T>(
  items: T[],
  x: (t: T) => number,
  halfWidth: (t: T) => number,
  priority: (t: T) => number,
  gap = 8,
  reserved: [number, number][] = [],
): T[] {
  const taken: [number, number][] = [...reserved]
  const kept: T[] = []
  for (const it of [...items].sort((a, b) => priority(b) - priority(a))) {
    const lo = x(it) - halfWidth(it) - gap / 2
    const hi = x(it) + halfWidth(it) + gap / 2
    if (taken.some(([a, b]) => lo < b && hi > a)) continue
    taken.push([lo, hi])
    kept.push(it)
  }
  return kept.sort((a, b) => x(a) - x(b))
}

type Stop = [number, [number, number, number]]

/** Dark theme: the noise floor melts into the panel and power climbs toward white. */
const HEAT_DARK: Stop[] = [
  [0.0, [8, 11, 16]],
  [0.35, [23, 55, 94]],
  [0.6, [34, 139, 110]],
  [0.8, [200, 170, 60]],
  [1.0, [255, 246, 235]],
]

/** Light theme: the same hue order, lightness reversed, so the floor melts into a white card and
 *  the strongest energy is the darkest ink on it. */
const HEAT_LIGHT: Stop[] = [
  [0.0, [250, 251, 253]],
  [0.35, [160, 200, 232]],
  [0.6, [22, 150, 120]],
  [0.8, [212, 130, 20]],
  [1.0, [120, 30, 20]],
]

/** Perceptually-ordered ramp for the waterfall: floor -> blue -> green -> amber -> peak. */
export function heat(t: number, light = false): [number, number, number] {
  const x = Math.max(0, Math.min(1, t))
  const stops = light ? HEAT_LIGHT : HEAT_DARK
  for (let i = 0; i < stops.length - 1; i++) {
    const [a, ca] = stops[i]
    const [b, cb] = stops[i + 1]
    if (x >= a && x <= b) {
      const u = (x - a) / (b - a)
      return [
        Math.round(ca[0] + u * (cb[0] - ca[0])),
        Math.round(ca[1] + u * (cb[1] - ca[1])),
        Math.round(ca[2] + u * (cb[2] - ca[2])),
      ]
    }
  }
  return stops[stops.length - 1][1]
}
