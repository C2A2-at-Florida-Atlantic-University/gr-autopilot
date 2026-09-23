import type { Recording } from "@/types"

// Injected at build time by vite.config.ts: the baked recording for the single-file artifact
// build, or `null` for the default build (which polls /data live).
declare global {
  const __GRA_REC__: Recording | null
}

export {}
