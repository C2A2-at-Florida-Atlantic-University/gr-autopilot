import type { DevicesPayload, PathCheck, TopologyPayload } from "@/types"

/**
 * The declared bench, drawn: one box per declared radio, one edge per declared path segment, the
 * containment drawn around them, and the Tier-B measurements written ON the edges they verify.
 *
 * The drawing is generated from /topology and /devices, never from a hand-placed picture, so a
 * bench file that names a fourth radio or a splitter gets drawn too. Live state (is the radio
 * answering, is the interferer keyed) comes from /devices on every poll; the declaration is fixed
 * at daemon startup.
 */
const W = 920
const H = 440
const BOX_W = 200
const BOX_H = 84

const ROLE_SHORT: Record<string, string> = { transmitter: "TX", receiver: "RX", jammer: "JAM", monitor: "MON" }
const ROLE_LIVE: Record<string, string> = { transmitter: "tx", receiver: "rx", monitor: "monitor" }
const ROLE_COLOR: Record<string, string> = {
  transmitter: "var(--primary)", receiver: "var(--spectrum)", jammer: "var(--warn)", monitor: "var(--muted-foreground)",
}

interface Node {
  id: string
  role: string
  kind?: string
  chip?: string | null
  uri?: string | null
  serial?: string | null
  x: number
  y: number
  device: boolean
}

const nodeOf = (endpoint: string) => endpoint.split(".")[0]
const portOf = (endpoint: string) => endpoint.split(".").slice(1).join(".")

/** Where a segment leaves a box: intersect the centre-to-centre line with the box edge. */
function anchor(from: Node, to: Node): [number, number] {
  const dx = to.x - from.x, dy = to.y - from.y
  if (dx === 0 && dy === 0) return [from.x, from.y]
  const t = Math.min(BOX_W / 2 / Math.max(Math.abs(dx), 1e-9), BOX_H / 2 / Math.max(Math.abs(dy), 1e-9))
  return [from.x + dx * t, from.y + dy * t]
}

function Arcs({ x, y, angle, color }: { x: number; y: number; angle: number; color: string }) {
  // three growing arcs facing along the segment: energy radiating into the medium
  return (
    <g fill="none" stroke={color} strokeWidth={1.2} opacity={0.7}>
      {[9, 16, 23].map((r) => {
        const a0 = angle - Math.PI / 4.2, a1 = angle + Math.PI / 4.2
        const x0 = x + r * Math.cos(a0), y0 = y + r * Math.sin(a0)
        const x1 = x + r * Math.cos(a1), y1 = y + r * Math.sin(a1)
        return <path key={r} d={`M${x0} ${y0} A${r} ${r} 0 0 1 ${x1} ${y1}`} />
      })}
    </g>
  )
}

function Antenna({ x, y, color }: { x: number; y: number; color: string }) {
  return (
    <g stroke={color} strokeWidth={1.5} fill="none" strokeLinecap="round">
      <path d={`M${x} ${y} v-18`} />
      <path d={`M${x - 7} ${y - 26} L${x} ${y - 18} L${x + 7} ${y - 26}`} />
    </g>
  )
}

export function TopologyGraph({ topology, devices, path }: {
  topology: TopologyPayload
  devices: DevicesPayload | null
  path: PathCheck | null | undefined
}) {
  const devs = topology.devices ?? {}
  const att = (topology.attestation ?? {}) as Record<string, unknown>
  const wireless = att.antennas_attached === true || (topology.path ?? []).some((s) => s.medium === "free_space")
  const enclosed = wireless || att.shielded === true
  const live = devices?.devices ?? {}
  const jam = devices?.jammer

  // ---- nodes: declared radios by role, then any passive element the path names -------------
  const nodes: Record<string, Node> = {}
  let others = 0
  for (const [name, d] of Object.entries(devs)) {
    const [x, y] =
      d.role === "transmitter" ? [190, 150]
      : d.role === "receiver" ? [730, 220]
      : d.role === "jammer" ? [190, 330]
      : [460, 70 + 100 * others++]
    nodes[name] = { id: name, role: d.role, kind: d.kind, chip: d.expect_chip, uri: d.uri, serial: d.expect_serial, x, y, device: true }
  }
  const segs = topology.path ?? []
  let passive = 0
  for (const s of segs) for (const e of [s.from, s.to]) {
    const id = nodeOf(e)
    if (!nodes[id]) nodes[id] = { id, role: "passive", x: 460, y: 240 + 90 * passive++, device: false }
  }

  // ---- edges: one per declared segment, carrying the measurement that verified it ----------
  const edges = segs.map((s, i) => {
    const a = nodes[nodeOf(s.from)], b = nodes[nodeOf(s.to)]
    const [x0, y0] = anchor(a, b), [x1, y1] = anchor(b, a)
    const isLink = a.role === "transmitter" && b.role === "receiver"
    const isJam = a.role === "jammer"
    let measure = "", ok: boolean | null = null
    if (path && isLink && path.link_snr_db !== null) {
      measure = `link ${path.link_snr_db} dB` + (path.link_ber !== null ? ` · BER ${path.link_ber.toExponential(1)}` : "")
      ok = path.link_ok
    } else if (path && isJam && path.jammer_margin_db !== null) {
      measure = `jammer +${path.jammer_margin_db} dB median`
        + (path.jammer_margin_spread_db != null ? ` · spread ${path.jammer_margin_spread_db} dB` : "")
      ok = path.jammer_ok
    }
    const color = ok === null ? "var(--muted-foreground)" : ok ? "var(--good)" : "var(--bad)"
    const medium = s.medium.replace("_", " ") + (s.loss_db != null ? ` · ${s.loss_db} dB` : "")
    return { key: i, a, b, x0, y0, x1, y1, color, medium, measure, free: s.medium === "free_space",
             fromPort: portOf(s.from), toPort: portOf(s.to) }
  })

  return (
    // Capped rather than free-scaling. At `w-full` the 920x440 viewBox took its height from the
    // container width, so on a wide window the bench diagram grew past 600px tall and pushed the
    // verdicts and findings — the parts you act on — below the fold. preserveAspectRatio letterboxes
    // it inside the cap, so the drawing stays centred and legible at any width.
    <svg viewBox={`0 0 ${W} ${H}`} className="mx-auto block h-auto w-full max-h-[min(38vh,340px)]" role="img"
         aria-label="bench topology: declared radios, the path between them, and what was measured on it">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10z" fill="context-stroke" />
        </marker>
      </defs>

      {/* containment: the thing that makes keying a transmitter legal */}
      {enclosed && (
        <g>
          <rect x={52} y={38} width={W - 104} height={H - 76} rx={18}
                fill="var(--muted)" fillOpacity={0.35} stroke="var(--border)" strokeWidth={1.5} strokeDasharray="8 6" />
          {/* absorber wedges along the inside of the walls */}
          <g fill="var(--border)" opacity={0.7}>
            {Array.from({ length: 33 }, (_, i) => 66 + i * 24).map((x) => (
              <g key={x}>
                <path d={`M${x} 40 l6 9 l6 -9z`} />
                <path d={`M${x} ${H - 40} l6 -9 l6 9z`} />
              </g>
            ))}
          </g>
          <text x={72} y={64} fontSize={11} fontFamily="ui-monospace, monospace" fill="var(--muted-foreground)">
            {String(att.medium ?? "enclosure")}
            {att.shielded === true ? " · shielded" : att.shielded === false ? " · NOT shielded" : ""}
            {att.date ? ` · attested ${String(att.date)}` : ""}
          </text>
        </g>
      )}

      {/* edges under the boxes */}
      {edges.map((e) => {
        const ang = Math.atan2(e.y1 - e.y0, e.x1 - e.x0)
        const mx = (e.x0 + e.x1) / 2, my = (e.y0 + e.y1) / 2
        return (
          <g key={e.key}>
            <line x1={e.x0} y1={e.y0} x2={e.x1} y2={e.y1} stroke={e.color} strokeWidth={1.8}
                  strokeDasharray={e.free ? "7 6" : undefined} markerEnd="url(#arrow)" />
            {e.free && <Arcs x={e.x0} y={e.y0} angle={ang} color={e.color} />}
            <text x={mx} y={my - 9} textAnchor="middle" fontSize={11} fontFamily="ui-monospace, monospace"
                  fill="var(--muted-foreground)" stroke="var(--background)" strokeWidth={4} paintOrder="stroke">
              {e.medium}
            </text>
            {e.measure && (
              <text x={mx} y={my + 15} textAnchor="middle" fontSize={11.5} fontWeight={600}
                    fontFamily="ui-monospace, monospace" fill={e.color}
                    stroke="var(--background)" strokeWidth={4} paintOrder="stroke">
                {e.measure}
              </text>
            )}
          </g>
        )
      })}

      {/* nodes */}
      {Object.values(nodes).map((n) => {
        const x = n.x - BOX_W / 2, y = n.y - BOX_H / 2
        const color = ROLE_COLOR[n.role] ?? "var(--muted-foreground)"
        const lv = n.role === "jammer" ? null : live[ROLE_LIVE[n.role] ?? ""]
        const alive: boolean | null = n.role === "jammer" ? (jam ? !!jam.available : null) : lv ? lv.alive : null
        const armed = n.role === "jammer" && !!jam?.armed
        const stateText = n.role === "jammer"
          ? (armed ? "TRANSMITTING" : jam?.enabled === false ? "disabled" : jam?.available ? "idle" : "not found")
          : lv ? (lv.alive ? (lv.temp_c !== undefined ? `up · ${lv.temp_c}°C` : "up") : "DOWN") : "—"
        const stateColor = armed ? "var(--bad)" : alive === true ? "var(--good)" : alive === false ? "var(--bad)" : "var(--muted-foreground)"
        if (!n.device) {
          return (
            <g key={n.id}>
              <rect x={x + 40} y={y + 22} width={BOX_W - 80} height={BOX_H - 44} rx={6}
                    fill="var(--card)" stroke="var(--border)" />
              <text x={n.x} y={n.y + 4} textAnchor="middle" fontSize={12} fontFamily="ui-monospace, monospace" fill="var(--foreground)">{n.id}</text>
            </g>
          )
        }
        return (
          <g key={n.id}>
            {wireless && <Antenna x={x + BOX_W - 18} y={y} color={color} />}
            <rect x={x} y={y} width={BOX_W} height={BOX_H} rx={10}
                  fill="var(--card)" stroke={armed ? "var(--bad)" : "var(--border)"} strokeWidth={armed ? 2 : 1.2} />
            <rect x={x} y={y} width={5} height={BOX_H} rx={2} fill={color} />
            <text x={x + 16} y={y + 20} fontSize={11} fontWeight={700} letterSpacing={1.5}
                  fontFamily="ui-monospace, monospace" fill={color}>{ROLE_SHORT[n.role] ?? n.role.toUpperCase()}</text>
            <text x={x + 52} y={y + 20} fontSize={13} fontWeight={600} fontFamily="ui-monospace, monospace" fill="var(--foreground)">{n.id}</text>
            <text x={x + 16} y={y + 40} fontSize={11} fontFamily="ui-monospace, monospace" fill="var(--muted-foreground)">
              {n.kind}{n.chip ? ` · ${n.chip}` : ""}{n.uri ? ` · ${n.uri}` : n.kind === "hackrf" ? " · usb" : ""}
            </text>
            <text x={x + 16} y={y + 56} fontSize={10} fontFamily="ui-monospace, monospace" fill="var(--muted-foreground)" opacity={0.75}>
              {n.serial ? `sn …${n.serial.slice(-12)}` : "serial not declared"}
            </text>
            <circle cx={x + 20} cy={y + 71} r={4} fill={stateColor}>
              {armed && <animate attributeName="opacity" values="1;0.25;1" dur="1.2s" repeatCount="indefinite" />}
            </circle>
            <text x={x + 30} y={y + 75} fontSize={11} fontWeight={armed ? 700 : 500}
                  fontFamily="ui-monospace, monospace" fill={stateColor}>{stateText}</text>
          </g>
        )
      })}
    </svg>
  )
}
