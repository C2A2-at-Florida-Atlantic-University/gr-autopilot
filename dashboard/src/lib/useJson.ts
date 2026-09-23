import { useEffect, useState } from "react"

export interface JsonState<T> {
  data: T | null
  error: string | null
  loading: boolean
  at: number | null // when the data was last fetched (ms epoch)
  refresh: () => void
}

/** Fetch one JSON endpoint, optionally polling. Only the page on screen polls its endpoint, so
 *  the daemon is never asked for eight things at once by a page showing one. */
export function useJson<T>(url: string | null, pollMs = 0): JsonState<T> {
  const [state, set] = useState<Omit<JsonState<T>, "refresh">>({
    data: null, error: null, loading: !!url, at: null,
  })
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    if (!url) return
    let alive = true
    const tick = async () => {
      try {
        const r = await fetch(url, { cache: "no-store" })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const d = (await r.json()) as T
        if (alive) set({ data: d, error: null, loading: false, at: Date.now() })
      } catch (e) {
        if (alive) set((s) => ({ ...s, error: (e as Error).message || String(e), loading: false }))
      }
    }
    tick()
    if (!pollMs) return () => { alive = false }
    const h = setInterval(tick, pollMs)
    return () => { alive = false; clearInterval(h) }
  }, [url, pollMs, nonce])
  return { ...state, refresh: () => setNonce((n) => n + 1) }
}
