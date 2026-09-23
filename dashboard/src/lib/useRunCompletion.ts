import { useEffect, useRef, useState } from "react"
import type { Telemetry } from "@/lib/telemetry"
import { buildReport, type RunReport } from "@/lib/report"

/**
 * Noticing that a run has finished.
 *
 * This is not inferred, because there is no way to see an ending from outside: a ledger that has
 * stopped growing is a finished run and an agent reading a constellation, writing an annotation or
 * thinking about the next rung, and on the radios those pauses routinely run to minutes. Keying
 * the report to quiet would raise it in the MIDDLE of a ladder — and then again, and again, as
 * every trial that landed afterwards re-armed the same guess. An interim report is worse than
 * none: it invites the operator to act on a number the rest of the run will move.
 *
 * So the agent says when it is done. `finish_experiment` stamps `ended_at` on the experiment, and
 * that stamp — nothing else — raises the report. It is a signal, not a state: we fire on the
 * TRANSITION to a newly stamped value, never on the mere fact that an experiment is closed. That
 * distinction is what keeps opening the console on a run that finished yesterday quiet.
 *
 * Appending to a concluded experiment clears the stamp server-side, so a run that turns out to
 * have more to measure carries on and its true ending raises a second, later report.
 */
export interface RunCompletion {
  report: RunReport | null
  open: boolean
  dismiss: () => void
  /** Re-open the last report of this session. The popup is now a once-per-run event, so losing it
   *  to a stray click would mean losing it for good. */
  reopen: () => void
  /** A report exists to be re-opened (the run has concluded at least once in this session). */
  available: boolean
}

export function useRunCompletion(tele: Telemetry): RunCompletion {
  const [report, setReport] = useState<RunReport | null>(null)
  const [open, setOpen] = useState(false)

  // Latest telemetry, read at fire time rather than captured in a closure.
  const latest = useRef(tele)
  latest.current = tele

  // Which experiment we are watching, and the `ended_at` we have already accounted for. A slug we
  // have never seen is baselined rather than fired on: the console may well be opened on a run
  // that ended long before the page did.
  const seen = useRef<{ slug: string | null; endedAt: number | null }>({ slug: null, endedAt: null })

  // Read off the two fields the trigger depends on, not the whole experiment object: /data hands
  // back a fresh object on every poll, so depending on it would re-run this effect twice a second.
  const slug = tele.experiment?.slug ?? null
  const endedAt = typeof tele.experiment?.ended_at === "number" ? tele.experiment.ended_at : null

  useEffect(() => {
    if (tele.mode !== "live") return
    const s = seen.current

    if (slug !== s.slug) {
      // A different experiment is selected (or the first one has appeared). Adopt its current
      // state as the baseline without reporting on it — switching experiments is not an ending,
      // and whatever this one's `ended_at` says happened before we were watching.
      seen.current = { slug, endedAt }
      return
    }
    if (endedAt === s.endedAt) return
    seen.current = { slug, endedAt }
    // Cleared: the run was concluded and then carried on. Nothing to report yet; the next
    // conclusion will carry a new timestamp and fire.
    if (endedAt === null) return

    const t = latest.current
    const rep = buildReport(t.ledger, t.snapshot, t.experiment, t.devices)
    if (!rep) return
    setReport(rep)
    setOpen(true)
  }, [tele.mode, slug, endedAt])

  return {
    report,
    open,
    // Dismissal closes the popup but keeps the report, so it can be re-opened from the header.
    // It is dropped only when a later run concludes and replaces it.
    dismiss: () => setOpen(false),
    reopen: () => { if (report) setOpen(true) },
    available: report !== null,
  }
}
