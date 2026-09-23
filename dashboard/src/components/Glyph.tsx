/** Crisp drawn glyphs (design review #8) — replace the emoji UI markers with instrument-grade line
 * icons that inherit currentColor, so the fault palette still drives them. 16×16 viewBox. */
type Name =
  | "clean" | "phase_offset" | "carrier_unlocked" | "low_snr" | "interference"
  | "eye" | "play" | "pause" | "reset" | "target" | "sun" | "moon"
  | "download" | "close" | "doc" | "table" | "image" | "flowgraph"
  | "comfortable" | "compact" | "trash"

const S = { fill: "none", stroke: "currentColor", strokeWidth: 1.5, strokeLinecap: "round", strokeLinejoin: "round" } as const

function Body({ name }: { name: Name }) {
  switch (name) {
    case "clean":
      return <path d="M3 8.4l3.3 3.3L13 5" {...S} strokeWidth={1.7} />
    case "phase_offset": // rotation off the grid
      return (
        <>
          <path d="M12.6 5.8A5 5 0 1 0 13 9.2" {...S} />
          <path d="M12.9 3.2v2.9h-2.9" {...S} />
        </>
      )
    case "carrier_unlocked": // a spinning ring
      return (
        <>
          <circle cx="8" cy="8" r="5" {...S} />
          <circle cx="8" cy="8" r="1.3" fill="currentColor" stroke="none" />
        </>
      )
    case "low_snr": // a fuzzy cloud of samples
      return (
        <g fill="currentColor" stroke="none">
          {[[5, 6, 1], [8, 5, 0.8], [11, 7, 1], [6, 9, 0.9], [9, 10, 1.1], [11, 10, 0.7], [7, 7, 0.7], [10, 4, 0.7]].map(
            ([x, y, r], i) => <circle key={i} cx={x} cy={y} r={r} opacity={0.55 + 0.12 * (i % 3)} />,
          )}
        </g>
      )
    case "interference": // a bolt
      return <path d="M9 1.5L3.8 8.7H7.4l-1 5.8 5.6-7.6H8.1z" fill="currentColor" stroke="none" />
    case "eye":
      return (
        <>
          <path d="M1.2 8S3.8 3.4 8 3.4 14.8 8 14.8 8 12.2 12.6 8 12.6 1.2 8 1.2 8z" {...S} />
          <circle cx="8" cy="8" r="2.1" {...S} />
        </>
      )
    case "play":
      return <path d="M5 3.5l7 4.5-7 4.5z" fill="currentColor" stroke="none" />
    case "pause":
      return <path d="M5.5 3.5v9M10.5 3.5v9" {...S} strokeWidth={2} />
    case "reset":
      return (
        <>
          <path d="M3.4 8a4.6 4.6 0 1 0 1.3-3.2" {...S} />
          <path d="M3 3.2v3.1h3.1" {...S} />
        </>
      )
    case "sun":
      return (
        <>
          <circle cx="8" cy="8" r="2.7" {...S} />
          <path d="M8 1.7v1.6M8 12.7v1.6M1.7 8h1.6M12.7 8h1.6M3.5 3.5l1.1 1.1M11.4 11.4l1.1 1.1M3.5 12.5l1.1-1.1M11.4 4.6l1.1-1.1" {...S} />
        </>
      )
    case "moon":
      return <path d="M13.3 9.7A5.6 5.6 0 1 1 6.3 2.7a4.5 4.5 0 0 0 7 7z" {...S} />
    case "target":
      return (
        <>
          <circle cx="8" cy="8" r="5.2" {...S} />
          <circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none" />
        </>
      )
    // ---- report surface: saving things and naming what they are ----
    case "download":
      return (
        <>
          <path d="M8 2.4v7.2" {...S} />
          <path d="M5.1 6.9L8 9.8l2.9-2.9" {...S} />
          <path d="M2.8 11.6v1.1a.9.9 0 0 0 .9.9h8.6a.9.9 0 0 0 .9-.9v-1.1" {...S} />
        </>
      )
    case "close":
      return <path d="M4 4l8 8M12 4l-8 8" {...S} strokeWidth={1.7} />
    case "doc":
      return (
        <>
          <path d="M9 1.8H4.6a.9.9 0 0 0-.9.9v10.6a.9.9 0 0 0 .9.9h6.8a.9.9 0 0 0 .9-.9V4.8z" {...S} />
          <path d="M9 1.8v3h3.3M5.9 8.4h4.2M5.9 10.7h4.2" {...S} />
        </>
      )
    case "table":
      return (
        <>
          <rect x="2.4" y="3.2" width="11.2" height="9.6" rx="1" {...S} />
          <path d="M2.4 6.4h11.2M6.4 6.4v6.4M10 6.4v6.4" {...S} />
        </>
      )
    case "image":
      return (
        <>
          <rect x="2.2" y="3" width="11.6" height="10" rx="1.1" {...S} />
          <circle cx="5.9" cy="6.4" r="1.1" {...S} />
          <path d="M2.8 11.2l3.1-2.8 2.4 2.1 2.2-2.4 2.7 3" {...S} />
        </>
      )
    // Density: the same three rows, breathing or packed. The pair reads as one control because
    // only the spacing differs — which is exactly what the setting changes.
    case "comfortable":
      return <path d="M2.6 4h10.8M2.6 8h10.8M2.6 12h10.8" {...S} />
    case "compact":
      return <path d="M2.6 5.2h10.8M2.6 8h10.8M2.6 10.8h10.8" {...S} />
    case "trash":
      return (
        <>
          <path d="M2.8 4.3h10.4" {...S} />
          <path d="M6.2 4.3V3.1a.9.9 0 0 1 .9-.9h1.8a.9.9 0 0 1 .9.9v1.2" {...S} />
          <path d="M4.1 4.3l.6 8.5a.9.9 0 0 0 .9.85h4.8a.9.9 0 0 0 .9-.85l.6-8.5" {...S} />
          <path d="M6.8 6.9v4.1M9.2 6.9v4.1" {...S} />
        </>
      )
    case "flowgraph":
      return (
        <>
          <rect x="1.6" y="5.9" width="4" height="4.2" rx="0.9" {...S} />
          <rect x="10.4" y="5.9" width="4" height="4.2" rx="0.9" {...S} />
          <path d="M5.6 8h4.8" {...S} />
          <path d="M8.9 6.7L10.5 8l-1.6 1.3" {...S} />
        </>
      )
  }
}

export function Glyph({ name, size = 14, className }: { name: Name; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" className={className} aria-hidden focusable="false">
      <Body name={name} />
    </svg>
  )
}

export const FAULT_GLYPH: Record<string, Name> = {
  clean: "clean", phase_offset: "phase_offset", carrier_unlocked: "carrier_unlocked",
  low_snr: "low_snr", interference: "interference",
}
