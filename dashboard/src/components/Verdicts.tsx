import type { PathCheck } from "@/types"

export const CHIP = "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider"

/** Three verdict states, not two. "Never checked" is not "checked and clean". */
export function IdentityVerdict({ verified }: { verified: boolean | null | undefined }) {
  if (verified === true) return <span className={`${CHIP} bg-good/15 text-good`}>identity verified</span>
  if (verified === false) return <span className={`${CHIP} bg-bad/15 text-bad`}>identity mismatch</span>
  return <span className={`${CHIP} bg-muted text-muted-foreground`}>identity not checked</span>
}

export function PathVerdict({ path }: { path: PathCheck | null | undefined }) {
  if (!path) return <span className={`${CHIP} bg-muted text-muted-foreground`}>path not measured</span>
  const cls = path.ok ? "bg-good/15 text-good" : "bg-bad/15 text-bad"
  const link = path.link_snr_db !== null ? ` · link ${path.link_snr_db} dB` : ""
  const jam = path.jammer_margin_db !== null ? ` · jammer +${path.jammer_margin_db} dB` : ""
  return <span className={`${CHIP} ${cls}`}>{path.ok ? "path verified" : "path failed"}{link}{jam}</span>
}
