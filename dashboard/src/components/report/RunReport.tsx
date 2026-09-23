import { useEffect, useMemo, useRef, useState } from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { Glyph } from "@/components/Glyph"
import { Constellation } from "@/components/Constellation"
import { Trends } from "@/components/Trends"
import { LadderFigure } from "@/components/report/LadderFigure"
import { EvmFigure } from "@/components/report/EvmFigure"
import { Figure } from "@/components/report/Figure"
import { canvasIn, downloadCanvasPng, downloadText, figureFilename, slugify } from "@/lib/figures"
import { fmtBer, fmtDate, fmtDuration, fmtHz, fmtInt, outcomeOf, toCsv, toMarkdown, type RunReport as Report } from "@/lib/report"
import type { FlowgraphsPayload } from "@/types"

/**
 * The report a completed run puts in front of you.
 *
 * Without it a run would end by simply going quiet: the console showing the last frame, the
 * ledger keeping its rows, and whether the thing had succeeded left to whoever could read a table
 * of ratios. This is that summary — figures first, then what can be taken away, then the
 * write-up — laid out as a short paper because that is the shape the conclusion actually has.
 *
 * It appears exactly once per run, when the agent declares the run over (`finish_experiment`),
 * and never while measuring is still going on. Dismissing it is not destructive — it can be
 * re-opened from the header until a later run replaces it. Nothing here blocks the console
 * underneath.
 */

function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: "good" | "bad" | "warn" }) {
  const c = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-foreground"
  return (
    <div className="min-w-0">
      <div className="text-[9.5px] uppercase tracking-[1.3px] text-muted-foreground">{label}</div>
      <div className={`truncate font-mono text-[15px] font-semibold ${c}`}>{value}</div>
    </div>
  )
}

function DownloadTile({ icon, title, sub, onClick, href, download }: {
  icon: "doc" | "table" | "image" | "flowgraph"
  title: string
  sub: string
  onClick?: () => void
  href?: string
  download?: string
}) {
  const inner = (
    <>
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border bg-muted/70 text-primary">
        <Glyph name={icon} size={14} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12.5px] font-medium text-foreground">{title}</span>
        <span className="block truncate font-mono text-[10.5px] text-muted-foreground">{sub}</span>
      </span>
      <span className="shrink-0 text-muted-foreground transition-colors group-hover/dl:text-primary">
        <Glyph name="download" size={13} />
      </span>
    </>
  )
  const cls = "group/dl flex items-center gap-2.5 rounded-lg border border-border bg-card/60 px-2.5 py-2 text-left " +
    "transition-colors hover:border-primary/40 hover:bg-primary/[0.06] " +
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
  if (href) return <a className={cls} href={href} download={download}>{inner}</a>
  return <button type="button" className={cls} onClick={onClick}>{inner}</button>
}

export function RunReportDialog({ report, open, onClose }: {
  report: Report | null
  open: boolean
  onClose: () => void
}) {
  const figures = useRef<HTMLDivElement>(null)
  const [grc, setGrc] = useState<{ file: string; dir: string } | null>(null)

  // The exported graph that belongs to this run. The list is fetched when the report opens
  // rather than polled: a finished run's artifacts do not change underneath it.
  useEffect(() => {
    if (!open || !report) return
    let alive = true
    fetch("./flowgraphs", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: FlowgraphsPayload) => {
        if (!alive) return
        const files = d.flowgraphs ?? []
        const want = report.settled?.structureIds[0] ?? report.finalStructureId ?? ""
        const stem = want.replace(/[^A-Za-z0-9_.-]+/g, "_")
        const hit = files.find((f) => f === `${stem}.grc`) ?? files.find((f) => f.startsWith(stem)) ?? files[0]
        setGrc(hit ? { file: hit, dir: d.dir } : null)
      })
      .catch(() => { if (alive) setGrc(null) })
    return () => { alive = false }
  }, [open, report])

  const slug = report ? slugify(`${report.experimentName}`) : "run"
  const md = useMemo(() => (report ? toMarkdown(report) : ""), [report])
  const csv = useMemo(() => (report ? toCsv(report) : ""), [report])

  if (!report) return null

  const saveAllFigures = () => {
    const nodes = figures.current?.querySelectorAll<HTMLElement>("figure[data-fig-n]") ?? []
    nodes.forEach((f, i) => {
      const c = canvasIn(f)
      if (!c) return
      const n = f.dataset.figN ?? String(i + 1)
      const title = f.dataset.figTitle ?? "figure"
      // Staggered: several synchronous downloads in one tick get collapsed by the browser.
      setTimeout(() => downloadCanvasPng(c, figureFilename(slug, `fig${n}-${title}`), `Figure ${n}. ${title}`, slug), i * 320)
    })
  }

  const settled = report.settled
  const blocked = report.blocked
  const outcome = outcomeOf(report)
  const ch = report.channel
  const decisions = report.decisions.filter((d) => d.verdict !== "conclusion")
  // A run that moved the link measured more than one channel, and the per-structure pool spans
  // both. It is evidence, not the verdict -- the before/after is.
  const acrossChannels = report.channel?.pivot != null
  const splitKnown = report.rungs.some((r) => r.hasSplit)
  // Without per-trial bit counts a ratio can only be the mean of the per-trial ratios. That is a
  // different quantity from a bit-weighted pool and must not be presented as one.
  const meansOnly = report.rungs.some((r) => !r.verdictAgg.pooled)

  return (
    <DialogPrimitive.Root open={open} onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-[var(--overlay)] backdrop-blur-[2px]" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-1/2 z-50 flex max-h-[92dvh] w-[min(96vw,1080px)] -translate-x-1/2
                     -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-border-strong
                     bg-background shadow-[0_40px_120px_-30px_var(--shadow)]"
        >
          {/* ---- masthead ------------------------------------------------------------------ */}
          <header className="relative shrink-0 border-b border-border bg-gradient-to-b from-card to-background px-6 pb-4 pt-5">
            <div
              aria-hidden
              className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary/60 to-transparent"
            />
            <div className="flex items-start gap-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[2.2px] text-primary">
                  <Glyph name="clean" size={11} />
                  Run report
                </div>
                <DialogPrimitive.Title className="mt-1 truncate text-[25px] font-semibold leading-tight tracking-[-0.3px]">
                  {report.experimentName}
                </DialogPrimitive.Title>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted-foreground">
                  <span
                    className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-semibold ${
                      report.isHardware ? "border-hardware/40 bg-hardware/10 text-hardware" : "border-border bg-muted/60"
                    }`}
                    title={report.isHardware ? "these numbers came from radios" : "these numbers came from a model"}
                  >
                    {report.isHardware ? "◉" : "◌"} {report.backend || "unknown backend"}
                  </span>
                  <span>{fmtDate(report.endedAt)}</span>
                  <span className="text-muted-foreground/50">·</span>
                  <span>{report.trials} trials</span>
                  <span className="text-muted-foreground/50">·</span>
                  <span>{report.rungs.length} structures</span>
                  <span className="text-muted-foreground/50">·</span>
                  <span>{fmtDuration(report.startedAt, report.endedAt)}</span>
                </div>
              </div>
              <DialogPrimitive.Close
                className="shrink-0 rounded-md border border-border bg-muted/60 p-1.5 text-muted-foreground
                           transition-colors hover:bg-border/60 hover:text-foreground
                           focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
                aria-label="Close report"
              >
                <Glyph name="close" size={14} />
              </DialogPrimitive.Close>
            </div>

            {/* The outcome of the WHOLE run, stated once, in the largest type on the page. Which
                four numbers those are depends on what the run was for — a ladder ends on a
                configuration, an avoidance run on a channel, a hopping run on a rate. */}
            <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3 rounded-xl border border-border bg-muted/40 px-4 py-3 sm:grid-cols-4">
              {outcome.stats.map((st) => (
                <Stat
                  key={st.label}
                  label={meansOnly && st.label === "Error ratio" ? "Error ratio (mean)" : st.label}
                  value={st.value}
                  tone={st.tone}
                />
              ))}
            </div>
            <p className="mt-2.5 text-[13px] leading-snug text-muted-foreground">{outcome.headline}</p>
          </header>

          {/* ---- body --------------------------------------------------------------------- */}
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-6 py-5">
            {/* figures first: the plate a reader looks at before any prose */}
            <section ref={figures} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Figure
                n={1}
                title="Error ratio by rung"
                runSlug={slug}
                caption={
                  <>Pooled over full-length trials, log axis, against the {report.targetBer.toExponential(0)} target
                    (amber). Ticks at the left of each bar are the individual trials; a rung marked
                    <span className="text-warn"> bimodal</span> has trials in both a passing and a failing cluster.</>
                }
              >
                <LadderFigure rungs={report.rungs} target={report.targetBer} />
              </Figure>

              <Figure
                n={2}
                title="Error vector by rung"
                runSlug={slug}
                caption={<>Mean, ±1 standard deviation and the full observed range. Read against Fig 1: an EVM
                  that does not grow while the error ratio does says the rung ran out of decision distance, not
                  signal-to-noise.</>}
              >
                <EvmFigure rungs={report.rungs} />
              </Figure>

              <Figure
                n={3}
                title="Trial history"
                runSlug={slug}
                caption={<>Every graded trial in order, as the run happened. BER is on a log axis with a separate
                  zero lane, because a trial with no errors has no position on a log scale.</>}
              >
                <Trends rows={report.ledger} targetBer={report.targetBer} />
              </Figure>

              <Figure
                n={4}
                title="Recovered constellation, final state"
                runSlug={slug}
                caption={
                  report.diagnosis
                    ? <>Density-shaded recovered symbols at the last measured state. Diagnosed{" "}
                        <span className="font-mono text-foreground">{report.diagnosis.fault}</span>{" "}
                        at confidence {report.diagnosis.confidence.toFixed(2)}.</>
                    : <>Density-shaded recovered symbols at the last measured state.</>
                }
              >
                <Constellation points={report.snapshot?.constellation ?? []} />
              </Figure>
            </section>

            {/* ---- downloadables ---------------------------------------------------------- */}
            <section className="mt-5">
              <h2 className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[1.6px] text-muted-foreground">
                <span className="h-px flex-1 bg-border" />
                Downloads
                <span className="h-px flex-1 bg-border" />
              </h2>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
                {grc ? (
                  <DownloadTile
                    icon="flowgraph"
                    title="Final flowgraph"
                    sub={grc.file}
                    href={`./flowgraphs/${encodeURIComponent(grc.file)}`}
                    download={grc.file}
                  />
                ) : (
                  <DownloadTile icon="flowgraph" title="Final flowgraph" sub="none exported" onClick={() => {}} />
                )}
                <DownloadTile
                  icon="doc"
                  title="Report"
                  sub={`${slug}.md`}
                  onClick={() => downloadText(`${slug}-report.md`, md, "text/markdown;charset=utf-8")}
                />
                <DownloadTile
                  icon="table"
                  title="Ledger"
                  sub={`${report.ledger.length} rows · csv`}
                  onClick={() => downloadText(`${slug}-ledger.csv`, csv, "text/csv;charset=utf-8")}
                />
                <DownloadTile
                  icon="image"
                  title="All figures"
                  sub="4 × png @2×"
                  onClick={saveAllFigures}
                />
              </div>
            </section>

            {/* ---- the write-up ----------------------------------------------------------- */}
            <section className="prose mt-6 max-w-none">
              <h2 className="!mt-0">Outcome</h2>
              <p><strong>{outcome.headline}</strong></p>
              {report.conclusion && (
                <blockquote className="!my-3 border-l-2 border-primary/50 pl-3 text-[13px] italic text-muted-foreground">
                  {report.conclusion}
                </blockquote>
              )}

              {/* What the agent CHANGED. Half of what a run did, and invisible in a table of
                  trials: without it a frequency-avoidance run reads as a handful of measurements
                  with no account of why the channel moved between them. */}
              {(ch || decisions.length > 0) && (
                <>
                  <h2>Decisions</h2>
                  {ch && (ch.fromHz !== null || ch.toHz !== null || ch.hop || ch.powerDb !== null) && (
                    <div className="not-prose mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border bg-muted/40 px-3.5 py-2.5 font-mono text-[12px]">
                      {(ch.fromHz !== null || ch.toHz !== null) && (
                        <span>
                          <span className="text-muted-foreground">channel </span>
                          {fmtHz(ch.fromHz)}
                          <span className="mx-1.5 text-primary">→</span>
                          <span className="font-semibold">{fmtHz(ch.toHz)}</span>
                          <span className="ml-1.5 text-muted-foreground">
                            ({ch.retunes} retune{ch.retunes === 1 ? "" : "s"})
                          </span>
                        </span>
                      )}
                      {(ch.before || ch.after) && (
                        <span title="Measured either side of the change. The two are not pooled: they are two different channels.">
                          <span className="text-muted-foreground">link </span>
                          <span className="text-bad">
                            {ch.before ? fmtBer(ch.before.ber, ch.before.berHi, ch.before.errors) : "—"}
                          </span>
                          <span className="mx-1.5 text-primary">→</span>
                          <span className="font-semibold text-good">
                            {ch.after ? fmtBer(ch.after.ber, ch.after.berHi, ch.after.errors) : "—"}
                          </span>
                        </span>
                      )}
                      {ch.hop && (
                        <span>
                          <span className="text-muted-foreground">hopping </span>
                          <span className="font-semibold">
                            {ch.hop.channelsHz.length} ch @ {fmtHz(ch.hop.rateHz)}
                          </span>
                          {ch.hopCleared && <span className="ml-1.5 text-muted-foreground">(cleared)</span>}
                        </span>
                      )}
                      {ch.powerDb !== null && (
                        <span>
                          <span className="text-muted-foreground">tx power </span>
                          <span className="font-semibold">
                            {ch.powerDb >= 0 ? "+" : ""}{ch.powerDb.toFixed(1)} dB
                          </span>
                        </span>
                      )}
                    </div>
                  )}
                  {ch && ch.sensed.length > 0 && (
                    <table>
                      <thead>
                        <tr><th>Candidate</th><th>Power over noise</th><th>Reading</th></tr>
                      </thead>
                      <tbody>
                        {ch.sensed.map((o, i) => (
                          <tr key={`${o.center_freq_hz ?? i}`}>
                            <td className="font-mono">{fmtHz(o.center_freq_hz ?? null)}</td>
                            <td className="font-mono">
                              {typeof o.power_db === "number" ? `${o.power_db.toFixed(1)} dB` : "—"}
                            </td>
                            <td>
                              <span className={o.occupied ? "text-bad" : "text-good"}>
                                {o.occupied ? "occupied" : "clear"}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {decisions.length > 0 && (
                    <ol className="not-prose mt-2 space-y-1 font-mono text-[12px]">
                      {decisions.map((d) => (
                        <li key={d.iteration} className="flex gap-2.5">
                          <span className="shrink-0 text-muted-foreground">#{d.iteration}</span>
                          <span className="min-w-0 text-foreground">{d.what}</span>
                        </li>
                      ))}
                    </ol>
                  )}
                </>
              )}

              <h2>Summary</h2>
              <p>
                {report.trials} graded trial{report.trials === 1 ? "" : "s"} across {report.rungs.length}{" "}
                structure{report.rungs.length === 1 ? "" : "s"} on{" "}
                <strong>{report.isHardware ? "real radios" : "a model"}</strong>
                {report.backend ? <> ({report.backend})</> : null}.{" "}
                {acrossChannels ? (
                  <>
                    The per-structure figures below pool trials from <strong>both channels</strong> and
                    are evidence, not the verdict on this run — the before/after above is.
                  </>
                ) : settled ? (
                  <>
                    The link carried <strong>{settled.label}</strong>
                    {settled.bps ? <> at {settled.bps} bits/symbol</> : null} with a pooled error ratio of{" "}
                    <strong>{fmtBer(settled.verdictAgg.ber, settled.verdictAgg.berHi, settled.verdictAgg.errors)}</strong>
                    {settled.verdictAgg.bits ? <> over {fmtInt(settled.verdictAgg.bits)} graded bits</> : null}, inside
                    the {report.targetBer.toExponential(0)} target.
                  </>
                ) : (
                  <>No structure met the {report.targetBer.toExponential(0)} target.</>
                )}
                {blocked && !acrossChannels && (
                  <>
                    {" "}The next rung, <strong>{blocked.label}</strong>, did not hold:{" "}
                    {fmtBer(blocked.verdictAgg.ber, blocked.verdictAgg.berHi, blocked.verdictAgg.errors)} pooled,
                    with {blocked.verdictAgg.passes} of {blocked.verdictAgg.trials} trials inside target.
                  </>
                )}
              </p>

              <h2>{report.rungs.length > 1 ? "Ladder" : "Structure"}</h2>
              <table>
                <thead>
                  <tr>
                    <th>Rung</th>
                    <th>b/sym</th>
                    <th>{splitKnown ? "Full-length" : "Trials"}</th>
                    <th>Pass</th>
                    <th>{meansOnly ? "Error ratio (mean)" : "Error ratio (pooled)"}</th>
                    <th>EVM mean ± sd</th>
                    <th>EVM range</th>
                    {splitKnown && <th>Short</th>}
                  </tr>
                </thead>
                <tbody>
                  {report.rungs.map((r) => {
                    const a = r.verdictAgg
                    return (
                      <tr key={r.label}>
                        <td className="whitespace-nowrap">
                          <span className={r.meets === true ? "text-good" : "text-bad"}>●</span>{" "}
                          <strong>{r.label}</strong>
                          {r.bimodal && <span className="ml-1.5 font-mono text-[10px] text-warn">bimodal</span>}
                          {!r.onFinalChannel && (
                            // The rung's verdict is inherited from a channel the run has left.
                            // Without the mark, a failure the interferer caused reads as the
                            // radio's ceiling on the channel the link is actually running now.
                            <span className="ml-1.5 font-mono text-[10px] text-warn"
                                  title="Last measured before the link moved — not re-established on the channel the run settled on">
                              pre-move
                            </span>
                          )}
                        </td>
                        <td className="font-mono">{r.bps ?? "—"}</td>
                        <td className="font-mono">{a.trials}</td>
                        <td className="font-mono">{a.passes}/{a.trials}</td>
                        <td className="font-mono">{fmtBer(a.ber, a.berHi, a.errors)}</td>
                        <td className="font-mono">
                          {a.evmMean === null ? "—" : `${a.evmMean.toFixed(2)} ± ${(a.evmSd ?? 0).toFixed(2)}%`}
                        </td>
                        <td className="font-mono">
                          {a.evmMin === null ? "—" : `${a.evmMin.toFixed(2)}–${(a.evmMax ?? 0).toFixed(2)}`}
                        </td>
                        {splitKnown && <td className="font-mono text-muted-foreground">{r.short.trials || "—"}</td>}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              {meansOnly && (
                <p className="!mt-[-6px] text-[11.5px] text-muted-foreground">
                  Ratios are the mean over trials. This ledger carries no per-trial bit counts, so they
                  cannot be weighted by bits, and a full-length trial cannot be told apart from a capture
                  that was cut short — both appear here as equals.
                </p>
              )}

              <h2>What the numbers say</h2>
              <ul>
                {blocked?.bimodal && !acrossChannels && (
                  <li>
                    <strong>{blocked.label} is bimodal, not merely degraded.</strong>{" "}
                    {blocked.verdictAgg.passes} of {blocked.verdictAgg.trials} full-length trials met the target
                    {blocked.bestBer !== null && <> (best {blocked.bestBer.toExponential(1)})</>}, while the rest
                    missed by more than an order of magnitude. A noise limit degrades smoothly; a rung that flips
                    between two states is an acquisition that either happens or does not — so the ceiling here is
                    reliability, and the hardware has already been shown to carry this rung at least once.
                  </li>
                )}
                {splitKnown && report.dropouts && report.dropouts.short > 0 && (
                  <li>
                    <strong>{report.dropouts.short} of {report.dropouts.total} trials returned a short capture</strong>{" "}
                    and are reported separately. A trial that graded fewer bits than were asked for is not the same
                    measurement as one that graded them all, so the pooled ratios above exclude them rather than
                    averaging them in.
                  </li>
                )}
                {report.diagnosis && (
                  <li>
                    <strong>Final diagnosis: <span className="font-mono">{report.diagnosis.fault}</span></strong>{" "}
                    at confidence {report.diagnosis.confidence.toFixed(2)} — {report.diagnosis.summary}.{" "}
                    Recommended action: {report.diagnosis.action}
                  </li>
                )}
                {settled && settled.verdictAgg.errors === 0 && settled.verdictAgg.bits ? (
                  <li>
                    <strong>{settled.label} produced no errors at all</strong> in {fmtInt(settled.verdictAgg.bits)}{" "}
                    graded bits. That bounds the ratio below {(3 / settled.verdictAgg.bits).toExponential(1)} at 95 %
                    confidence; it does not measure it.
                  </li>
                ) : null}
              </ul>

              <h2>Conclusion</h2>
              <p>
                {settled ? (
                  <>
                    Keep <strong>{settled.label}</strong>
                    {settled.bps ? <> ({settled.bps} bits/symbol)</> : null}.{" "}
                    {blocked?.bimodal
                      ? <>The rung above is reachable but not repeatable at this operating point, so the reliable
                         ceiling and the hardware ceiling are not the same number — treat {settled.label} as the
                         former, not the latter.</>
                      : blocked
                        ? <>{blocked.label} did not hold at this operating point.</>
                        : <>Nothing above it was tried.</>}
                  </>
                ) : (
                  <>Nothing met the target at this operating point; drop the ladder or add coding before climbing again.</>
                )}
              </p>
            </section>
          </div>

          {/* ---- footer -------------------------------------------------------------------- */}
          <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-border bg-card/60 px-6 py-3">
            <span className="min-w-0 truncate font-mono text-[10.5px] text-muted-foreground">
              framework-graded · ledger {report.ledger.length} rows
              {grc ? <> · {grc.dir.split("/").slice(-3).join("/")}</> : null}
            </span>
            <DialogPrimitive.Close
              className="h-8 shrink-0 rounded-md border border-border bg-muted px-3.5 text-[13px] font-medium
                         transition-colors hover:bg-border/60
                         focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
            >
              Dismiss
            </DialogPrimitive.Close>
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}
