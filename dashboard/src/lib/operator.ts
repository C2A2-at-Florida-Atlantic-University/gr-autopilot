import { toast } from "sonner"

/**
 * The operator surface (POST /control).
 *
 * Two things this owns. First, one place that posts, so the separate panels do not each keep
 * their own idea of the scenario. Second, feedback: on a bench with a real HackRF in the chamber,
 * arming a transmitter must not succeed or fail in silence. Every post says what happened, and a
 * 403 says specifically that the credential was rejected rather than reporting a generic failure.
 *
 * Authentication is the daemon's `--token`, the same bearer credential /mcp uses. A browser on
 * the bench host needs none of it: loopback is exempt, because a local process could read the
 * token out of the daemon's command line anyway. From any other machine the page sends it, and
 * it is held in memory for the page session rather than persisted — localStorage would leave a
 * credential that keys a transmitter sitting in the browser profile of whatever laptop last
 * opened the console.
 *
 * There is deliberately no separate operator credential keeping the agent off this surface: the
 * bench is driven over the network, so the agent holds the bearer token, and one credential
 * cannot separate two principals. The guarantee that matters is narrower and lives on the
 * server — GET /control does not report the hidden channel setpoint to anyone.
 */
export interface ControlCmd {
  running?: boolean
  reset?: boolean
  target_ber?: number
  es_n0_db?: number | null
  jammer?: boolean
  jammer_freq_hz?: number
  jammer_if_gain?: number
  jammer_kind?: string
  jammer_follow?: boolean
  tx_atten_db?: number
  rx_gain_db?: number
}

let token = ""

/** The bearer header, or nothing when no token has been entered (the loopback case). */
export function authHeaders(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export function accessToken(): string { return token }
export function setAccessToken(value: string) {
  // Tolerate what people actually paste: the whole header line, with or without the scheme.
  token = value.trim().replace(/^authorization\s*:\s*/i, "").replace(/^bearer\s+/i, "").trim()
}
export function hasAccessToken(): boolean { return token.length > 0 }

/** Current control state, or null when it cannot be read. */
export async function readControl(): Promise<Record<string, unknown> | null> {
  try {
    const r = await fetch("./control", {
      headers: { Accept: "application/json", ...authHeaders() },
    })
    if (!r.ok) return null
    const ct = r.headers.get("content-type") ?? ""
    if (!ct.includes("json")) return null   // unauthenticated GET serves the app shell
    return (await r.json()) as Record<string, unknown>
  } catch {
    return null
  }
}

/**
 * Post a command. Returns null on success and an error MESSAGE on failure — the contract the
 * existing panels are written against — and raises a toast either way.
 *
 * `describe` should say what actually happened in the world ("Jammer armed at 2370.000 MHz"),
 * not that a request succeeded.
 */
export async function postControl(cmd: ControlCmd, describe?: string): Promise<string | null> {
  try {
    const r = await fetch("./control", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(cmd),
    })
    const text = await r.text()
    let data: { error?: string } | null = null
    try { data = JSON.parse(text) } catch { /* tolerate a non-JSON body */ }

    if (r.status === 403) {
      const msg = "access token required"
      toast.error("Access token rejected", {
        description: data?.error
          ?? "From another machine the console needs the daemon's --token value.",
      })
      return msg
    }
    if (!r.ok) {
      const msg = data?.error ?? `HTTP ${r.status}`
      toast.error("Control refused", { description: msg })
      return msg
    }
    if (describe) toast.success(describe)
    return null
  } catch {
    const msg = "cannot reach the daemon"
    toast.error("Cannot reach the daemon", { description: "POST /control did not complete." })
    return msg
  }
}
