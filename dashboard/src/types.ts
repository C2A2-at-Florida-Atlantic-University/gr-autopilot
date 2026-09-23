export interface Metrics {
  ber: number
  evm_pct: number
  snr_db: number
  target_ber: number
  feasible: boolean
}

export interface Structure {
  modcod: string
  center_freq_hz?: number
  symbol_rate_hz?: number
}

export interface ActiveLoop {
  loop: "outer" | "inner"
  detail?: string
}

export interface Spectrum {
  // Normalized frequency, [-0.5, 0.5]. Kept normalized so an older snapshot still renders.
  freqs: number[]
  psd_db: number[]
  // Full width the normalized axis represents, in Hz. Because the transform is fed recovered
  // symbols (one sample per symbol), this is the SYMBOL rate, not the radio's sample rate.
  span_hz?: number
  // Radio frequency the axis is centred on, so ticks can read 2369.65 / 2370.00 / 2370.35 MHz
  // rather than a bare -0.5 / 0 / +0.5.
  center_hz?: number
}

export interface OccupancyChannel {
  center_freq_hz: number
  power_db: number // received power over the noise floor, link silent (energy detection)
  occupied: boolean
  detect_margin_db?: number // the backend's occupied threshold (sim 3 dB, Pluto 6 dB)
}

export interface TokenStats {
  total: number // ~tokens the LLM read/emitted through the MCP tool surface (a proxy)
  calls: number
  attempts: number
  this_attempt: number
  avg_per_attempt: number
  per_attempt: number[] // token cost of each completed attempt (for the trend)
  by_tool?: Record<string, number>
  estimated?: boolean
}

export interface Control {
  running: boolean
  target_ber: number
  es_n0_db: number | null // the hidden SNR the operator sets
  jammer: boolean
}

export interface Diagnosis {
  fault: "clean" | "phase_offset" | "carrier_unlocked" | "low_snr" | "interference"
  summary: string // what the agent SEES in the constellation
  action: string // the fix it recommends
  confidence: number
  features?: Record<string, number>
}

export interface Snapshot {
  t: number
  ts?: number // wall clock (epoch s) when this snapshot was written; the server ages it into age_s
  goal: string
  structure: Structure
  metrics: Metrics
  active_loop: ActiveLoop
  constellation: number[][]
  spectrum: Spectrum
  occupancy?: OccupancyChannel[] // present only during a monitor sweep (Stage 2)
  tokens?: TokenStats // present when the loop is driven over the MCP tool surface
  control?: Control // present when the operator console is driving (enables the controls panel)
  diagnosis?: Diagnosis // the agent's vision read of the constellation (perception.diagnose_signal)
}

export interface LedgerRow {
  iteration: number
  loop: string
  structure_id: string
  edit_description: string
  metrics: Record<string, number>
  verdict: string
  // A row is either a MEASUREMENT (metrics, no interesting params) or a DECISION the agent took —
  // a retune, a hop plan, a power change, a sweep of the band. Decisions carry no metrics, so
  // anything pooling error ratios skips them, and their detail lives in `params`.
  params?: Record<string, unknown>
  note?: string
}

export interface Frame {
  snapshot: Snapshot
  ledger: LedgerRow[]
}

export interface Recording {
  frames: Frame[]
}

/** The experiment the history is being written into. `backend` is the provenance line: whether
 *  these iterations came from radios or from a model, which no metric reveals on its own. */
export interface ExperimentInfo {
  name: string
  slug: string          // its directory under the runs dir
  dir?: string
  goal: string
  backend: string
  running: boolean
  started_at: number | null
  ended_at?: number | null
  iterations: number
  last_activity?: number | null
  structure_id?: string | null
}

/** What start/switch cleared, in the words the operator and the agent both receive. */
export interface ResetReport {
  reason?: string
  session: string[]
  hardware: string[]
  operator: string[]
}

export interface ExperimentAction {
  ok: boolean
  action: "started" | "switched" | "renamed" | "unchanged" | "deleted"
  experiment: ExperimentInfo
  reset?: ResetReport
}

export interface DataPayload {
  ledger: LedgerRow[]
  snapshot: Snapshot | null
  // Seconds since the snapshot was written (server-computed), or null when there is no snapshot.
  // The snapshot file outlives the run that wrote it, so this is what separates a live agent from
  // a stopped one.
  age_s?: number | null
  // The run these rows belong to. Null before any experiment has been started.
  experiment?: ExperimentInfo | null
}

export interface Device {
  uri: string
  alive: boolean
  temp_c?: number
  reason?: string // why it is not answering, when it is not
  worker?: string // "dead" when the process that owned this radio has gone
}

export interface DevicesPayload {
  devices: Record<string, Device> // keyed by role: "tx", "rx", ...
  backend: string // "pluto" | "simulated"
  note?: string
  error?: string
  worker?: { running: boolean; pid: number | null; reason: string; signal?: string }
  jammer?: JammerStatus
  path?: PathCheck | null
}

// The framework's interferer. Operator property: it never appears on the agent's tool surface,
// so the dashboard is the only place a human can see whether it is actually transmitting.
export interface JammerStatus {
  enabled: boolean
  available: boolean | null
  armed: boolean
  intended: boolean // what the operator last ASKED for
  error: string | null
  center_freq_hz: number
  if_gain: number
  kind: string
  follow: boolean
  retune_count?: number
  last_retune_latency_s?: number
}

// Tier-B verification: established by transmitting, not by asserting.
export interface PathCheck {
  ok: boolean
  link_ok: boolean
  link_snr_db: number | null
  link_ber: number | null
  jammer_ok: boolean | null
  jammer_margin_db: number | null // median over several senses
  jammer_margin_spread_db?: number | null // max - min over those senses; large = pulsed/intermittent
  expected_snr_db?: number | null
  findings: string[]
}

// The declared bench and whether the hardware agrees with it.
export interface TopologyPayload {
  declared: boolean
  note?: string
  name?: string
  source?: string
  sha256?: string
  link_band_hz?: [number, number] | null
  attestation?: Record<string, unknown>
  jammer_permitted?: boolean
  jammer_permitted_reason?: string
  identity_verified?: boolean | null // null = never checked, which is NOT the same as clean
  findings?: string[] | null
  reproducibility?: { recorded: boolean; missing: string[]; note: string }
  devices?: Record<string, { kind: string; role: string; uri: string | null;
                             expect_serial: string | null; expect_chip: string | null }>
  path?: PathSegment[]
  geometry?: Geometry
  version?: number
  error?: string
}

// What the page is actually showing right now:
//   live    — the agent measured something within the freshness window
//   idle    — the server is up but nothing has run recently (stale data, must not read as current)
//   offline — /data is unreachable
export type Liveness = "live" | "idle" | "offline"

// ---- portal pages: one per daemon endpoint --------------------------------------------------

export interface PathSegment {
  from: string // "<device>.<port>" or a passive element ("splitter", "20 dB attenuator")
  medium: string // "free_space" | "coax" | ...
  to: string
  loss_db?: number
}

export interface Geometry {
  separation_m: number | null
  jammer_separation_m: number | null
  antennas?: string
}

export interface Experiment {
  id: number
  name: string
  goal: string
  backend: string
  started_at: number // epoch seconds
  ended_at: number | null
  running: boolean
  iterations: number
  best_ber: number | null
  artifacts: Array<Record<string, unknown>>
}

export interface ExperimentsPayload {
  experiments: ExperimentInfo[]
  current?: string | null   // slug of the selected experiment, or null when none is
  runs_dir?: string
  db?: string               // the single-ledger form the scripts still serve
  error?: string
}

export interface FlowgraphsPayload {
  flowgraphs: string[]
  dir: string
  open_with: string
}

export interface PromptArg {
  name: string
  description: string
  default?: string
  required?: boolean
}

export interface Prompt {
  name: string
  title: string
  topic: string
  description: string
  arguments: PromptArg[]
  body: string
  notes?: string // operator-facing: what to arm, which number to trust; never part of the prompt
}

export interface PromptsPayload {
  prompts: Prompt[]
}

export interface McpToolParam {
  type?: string
  description?: string
  default?: unknown
  enum?: unknown[]
}

export interface McpTool {
  name: string
  description?: string
  inputSchema?: { properties?: Record<string, McpToolParam>; required?: string[] }
}

// One documentation page as the daemon renders it: /wiki/<name>?fragment=1
export interface WikiPageRef { name: string; title: string; url?: string; exists?: boolean }
export interface WikiFragment {
  name: string
  title: string
  body: string // rendered HTML, first h1 removed
  toc: string // rendered HTML list, may be empty
  pages: WikiPageRef[]
  prev: WikiPageRef | null
  next: WikiPageRef | null
}
