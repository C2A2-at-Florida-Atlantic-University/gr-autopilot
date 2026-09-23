import { useEffect, useRef, useState } from "react"
import type { DataPayload, DevicesPayload, ExperimentInfo, TopologyPayload, Frame, LedgerRow, Liveness, Snapshot } from "@/types"

export interface Telemetry {
  mode: "live" | "replay"
  snapshot: Snapshot | null
  ledger: LedgerRow[]
  connected: boolean
  liveness: Liveness
  ageS: number | null // seconds since the snapshot was written (null when there is none)
  devices: DevicesPayload | null // per-radio liveness, measured when asked (null in replay)
  topology: TopologyPayload | null // the declared bench + whether the hardware agrees with it
  // Which run this is, and whether it came from radios or a model. Survives an idle console: the
  // history has an identity even when nothing is being measured right now.
  experiment: ExperimentInfo | null
  // replay controls (no-ops in live mode):
  frames: Frame[]
  idx: number
  playing: boolean
  goTo: (i: number) => void
  toggle: () => void
}

const FRAME_MS = 1800

// How stale a snapshot may be and still count as live. A trial plus the mission's inter-trial
// pause is ~1-2 s on the sim backend, so 6 s tolerates a slow trial without ever letting a
// stopped run keep claiming LIVE. Deliberately generous: a false IDLE is a cosmetic blip, a false
// LIVE misrepresents the system.
export const STALE_AFTER_S = 6

function prefersReducedMotion() {
  return typeof window !== "undefined" &&
    !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
}

export function useTelemetry(pollMs = 600): Telemetry {
  const rec = __GRA_REC__
  const replay = !!rec && rec.frames.length > 0

  const [live, setLive] = useState<DataPayload>({ ledger: [], snapshot: null, age_s: null })
  const [devices, setDevices] = useState<DevicesPayload | null>(null)
  const [topology, setTopology] = useState<TopologyPayload | null>(null)
  const [connected, setConnected] = useState(false)
  const [idx, setIdx] = useState(0)
  const [playing, setPlaying] = useState(() => !prefersReducedMotion())
  const topologyFetched = useRef(false)
  const idxRef = useRef(0)
  idxRef.current = idx

  // Live mode: poll the stdlib server's /data endpoint.
  useEffect(() => {
    if (replay) return
    let alive = true
    const tick = async () => {
      try {
        const r = await fetch("./data", { cache: "no-store" })
        const d = (await r.json()) as DataPayload
        if (alive) { setLive(d); setConnected(true) }
      } catch {
        if (alive) setConnected(false)
        return
      }
      // Device liveness is a SEPARATE request because it is measured on demand -- each poll asks
      // the radios whether they are still there, rather than reporting what was true at startup.
      try {
        const r = await fetch("./devices", { cache: "no-store" })
        const d = (await r.json()) as DevicesPayload
        if (alive) setDevices(d)
      } catch {
        if (alive) setDevices(null)
      }
      // The declaration changes only when an operator edits it, so it is fetched once rather than
      // polled -- but the VERDICT against the hardware is what matters, and that is fixed at
      // daemon startup too.
      if (alive && !topologyFetched.current) {
        topologyFetched.current = true
        try {
          const r = await fetch("./topology", { cache: "no-store" })
          if (alive) setTopology((await r.json()) as TopologyPayload)
        } catch {
          if (alive) setTopology(null)
        }
      }
    }
    tick()
    const h = setInterval(tick, pollMs)
    return () => { alive = false; clearInterval(h) }
  }, [replay, pollMs])

  // Replay mode: auto-advance frames until the end.
  useEffect(() => {
    if (!replay || !playing) return
    const h = setInterval(() => {
      const next = idxRef.current + 1
      if (next >= rec!.frames.length) { setPlaying(false); return }
      setIdx(next)
    }, FRAME_MS)
    return () => clearInterval(h)
  }, [replay, playing, rec])

  if (replay) {
    const fr = rec!.frames[idx]
    return {
      mode: "replay",
      snapshot: fr.snapshot,
      ledger: fr.ledger,
      connected: true,
      // A baked recording is history by construction — never claim it is live.
      liveness: "idle",
      ageS: null,
      devices: null,          // a recording has no live radios behind it
      topology: null,
      experiment: null,       // ...nor a running experiment
      frames: rec!.frames,
      idx,
      playing,
      goTo: (i) => { setPlaying(false); setIdx(Math.max(0, Math.min(rec!.frames.length - 1, i))) },
      toggle: () => {
        if (idx >= rec!.frames.length - 1) { setIdx(0); setPlaying(true) }
        else setPlaying((p) => !p)
      },
    }
  }

  // Liveness is about the AGENT, not the web server: /data answering only proves the server is up,
  // and it outlives the mission loop. A snapshot older than the window (or none at all) means
  // nothing is running, so the page must say so rather than keep rendering the last frame.
  const ageS = live.age_s ?? null
  const liveness: Liveness = !connected
    ? "offline"
    : live.snapshot !== null && ageS !== null && ageS <= STALE_AFTER_S
      ? "live"
      : "idle"

  return {
    mode: "live",
    snapshot: live.snapshot,
    ledger: live.ledger,
    connected,
    liveness,
    ageS,
    devices,
    topology,
    experiment: live.experiment ?? null,
    frames: [],
    idx: 0,
    playing: false,
    goTo: () => {},
    toggle: () => {},
  }
}
