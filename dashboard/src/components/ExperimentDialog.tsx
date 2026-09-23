import { useEffect, useState } from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { Button } from "@/components/ui/button"
import { Glyph } from "@/components/Glyph"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useJson } from "@/lib/useJson"
import { experimentAction, isRemotePage } from "@/lib/experiments"
import { accessToken, setAccessToken } from "@/lib/operator"
import type { ExperimentInfo, ExperimentsPayload } from "@/types"

/**
 * Where an experiment gets its name.
 *
 * Opens on its own when the daemon has nothing selected (the GATE: measuring is refused until a
 * name exists, so the page asks rather than showing an empty console), and from the header chip
 * at any time to rename the current one, switch to a saved one, or start another.
 *
 * Starting and switching reset the session and the radios. The dialog says so before the button
 * is pressed, and the toast afterwards repeats what the server actually cleared.
 */
const inputCls = "h-8 w-full rounded-md border border-border bg-muted px-2.5 font-mono text-[13px] " +
  "text-foreground outline-none placeholder:text-muted-foreground/60 focus-visible:ring-2 focus-visible:ring-primary/60"

function fmtWhen(s: number | null | undefined) {
  return s ? new Date(s * 1000).toLocaleString() : "—"
}

export function ExperimentDialog({ open, onOpenChange, current, gate = false }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  current: ExperimentInfo | null
  gate?: boolean               // true when nothing is selected: the dialog cannot be dismissed
}) {
  const saved = useJson<ExperimentsPayload>(open ? "./experiments" : null)
  const [tab, setTab] = useState<string>(current ? "current" : "new")
  const [name, setName] = useState("")
  const [goal, setGoal] = useState("")
  const [newName, setNewName] = useState(current?.name ?? "")
  const [newGoal, setNewGoal] = useState(current?.goal ?? "")
  const [busy, setBusy] = useState(false)
  // The slug whose delete is awaiting confirmation. One row at a time: arming a second disarms
  // the first, so the armed row is always the one the pointer is on.
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [token, setToken] = useState(accessToken())
  const remote = isRemotePage()

  // Seed the fields when the dialog OPENS, and again only if the selected experiment genuinely
  // changes underneath it.
  //
  // Keyed on the experiment's IDENTITY, never on the `current` object. Telemetry re-parses its
  // JSON every 600 ms, so `current` is a different object on every tick even when nothing about
  // the experiment changed -- and `last_activity` changes for real. With the object in the
  // dependency list this effect re-ran almost twice a second, which is what reset the tab the
  // moment it was switched and erased whatever was being typed into it.
  const identity = current ? (current.slug || current.name || "current") : null
  useEffect(() => {
    if (!open) return
    setTab(identity ? "current" : "new")
    setNewName(current?.name ?? ""); setNewGoal(current?.goal ?? "")
    setName(""); setGoal(""); setConfirmDelete(null)
    // `current` is read here for its seed values but is deliberately NOT a dependency: see above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, identity])

  const others = (saved.data?.experiments ?? []).filter((e) => e.slug !== current?.slug)

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    try { const r = await fn(); if (r) onOpenChange(false) } finally { setBusy(false) }
  }

  // Deleting does not close the dialog: the list is where the operator is working, and tidying up
  // several old runs should not mean reopening it each time. The saved list is re-read afterwards
  // so the row disappears rather than lingering as a stale entry that 404s when clicked.
  const remove = async (slug: string) => {
    setBusy(true)
    try {
      const r = await experimentAction("delete", slug)
      if (r) { setConfirmDelete(null); saved.refresh() }
    } finally { setBusy(false) }
  }

  return (
    <DialogPrimitive.Root open={open} onOpenChange={(v) => { if (!gate || v) onOpenChange(v) }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-[var(--overlay)]" />
        <DialogPrimitive.Content
          onEscapeKeyDown={(e) => { if (gate) e.preventDefault() }}
          onPointerDownOutside={(e) => { if (gate) e.preventDefault() }}
          className="fixed left-1/2 top-1/2 z-50 flex w-[min(94vw,560px)] -translate-x-1/2 -translate-y-1/2 flex-col gap-3
                     rounded-xl border bg-card p-5 shadow-[0_24px_60px_-24px_var(--shadow)]"
        >
          <div>
            <DialogPrimitive.Title className="font-mono text-[13px] font-semibold uppercase tracking-[2px] text-muted-foreground">
              {gate ? "Name this experiment" : "Experiment"}
            </DialogPrimitive.Title>
            <DialogPrimitive.Description className="mt-1 text-[13px] text-muted-foreground">
              {gate
                ? "Nothing is selected, so nothing can be measured or recorded. Give it a name — it becomes the directory every result is written to."
                : <>Current: <span className="font-mono text-foreground">{current?.name}</span> · {current?.iterations ?? 0} iterations{current?.backend ? <> · {current.backend}</> : null}</>}
            </DialogPrimitive.Description>
          </div>

          {remote && (
            <div className="flex flex-col gap-1">
              <label className="text-[11px] uppercase tracking-wider text-muted-foreground">
                access token <span className="normal-case tracking-normal opacity-70">(required off the bench host; held in memory only)</span>
              </label>
              <input type="password" className={inputCls} value={token} autoComplete="off"
                     placeholder="the daemon's --token value"
                     onChange={(e) => { setToken(e.target.value); setAccessToken(e.target.value) }} />
            </div>
          )}

          <Tabs value={tab} onValueChange={setTab}>
            <TabsList>
              {current && <TabsTrigger value="current">rename</TabsTrigger>}
              <TabsTrigger value="switch">switch{others.length ? ` (${others.length})` : ""}</TabsTrigger>
              <TabsTrigger value="new">new</TabsTrigger>
            </TabsList>

            {current && (
              <TabsContent value="current" className="mt-3 flex flex-col gap-2">
                <label className="text-[11px] uppercase tracking-wider text-muted-foreground">name</label>
                <input className={inputCls} value={newName} onChange={(e) => setNewName(e.target.value)} />
                <label className="text-[11px] uppercase tracking-wider text-muted-foreground">goal</label>
                <input className={inputCls} value={newGoal} onChange={(e) => setNewGoal(e.target.value)} placeholder="one line on what this is for" />
                <p className="text-[12px] text-muted-foreground">Renaming moves the directory and keeps everything — history, built link, claims. Nothing is reset.</p>
                <div className="flex justify-end">
                  <Button variant="primary" size="sm" disabled={busy || !newName.trim()}
                          onClick={() => run(() => experimentAction("rename", newName, newGoal))}>
                    Rename
                  </Button>
                </div>
              </TabsContent>
            )}

            <TabsContent value="switch" className="mt-3 flex flex-col gap-2">
              {saved.loading && <p className="text-[12px] text-muted-foreground">Reading the runs directory…</p>}
              {!saved.loading && others.length === 0 && (
                <p className="text-[12px] text-muted-foreground">No other experiments under <span className="font-mono">{saved.data?.runs_dir ?? "runs/"}</span>.</p>
              )}
              {others.length > 0 && (
                <div className="max-h-[260px] overflow-auto rounded-md border">
                  {others.map((e) => (
                    <div key={e.slug} className="flex items-center gap-3 border-b px-3 py-2 last:border-b-0">
                      <div className="min-w-0 flex-1">
                        <div className="truncate font-mono text-[13px] text-foreground">{e.name}</div>
                        <div className="truncate text-[11px] text-muted-foreground">
                          {e.iterations} it · {e.backend || "no backend recorded"} · {fmtWhen(e.last_activity ?? e.started_at)}
                          {e.goal ? <> · {e.goal}</> : null}
                        </div>
                      </div>
                      {confirmDelete === e.slug ? (
                        // Two steps, never one. The confirm states WHAT is about to go, because
                        // an icon cannot carry "irreversible" and a row of old runs all look
                        // alike at a glance.
                        <div className="flex shrink-0 items-center gap-2">
                          <span className="whitespace-nowrap text-[11px] text-warn">
                            delete {e.iterations} iterations?
                          </span>
                          <Button size="sm" variant="ghost" disabled={busy}
                                  onClick={() => setConfirmDelete(null)}>
                            Cancel
                          </Button>
                          <Button size="sm" variant="danger" disabled={busy}
                                  onClick={() => remove(e.slug)}>
                            Delete
                          </Button>
                        </div>
                      ) : (
                        <div className="flex shrink-0 items-center gap-1.5">
                          <Button size="sm" variant="outline" disabled={busy}
                                  onClick={() => run(() => experimentAction("switch", e.slug))}>
                            Open
                          </Button>
                          <button
                            type="button"
                            disabled={busy}
                            aria-label={`Delete ${e.name}`}
                            title={`Delete ${e.name} and everything under ${e.dir}`}
                            onClick={() => setConfirmDelete(e.slug)}
                            className="rounded-md border border-transparent p-1.5 text-muted-foreground outline-none
                                       hover:border-bad/45 hover:bg-bad/10 hover:text-bad
                                       focus-visible:ring-2 focus-visible:ring-primary/60 disabled:opacity-45"
                          >
                            <Glyph name="trash" size={14} />
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
              <ResetNote verb="Switching" />
            </TabsContent>

            <TabsContent value="new" className="mt-3 flex flex-col gap-2">
              <label className="text-[11px] uppercase tracking-wider text-muted-foreground">name</label>
              <input className={inputCls} autoFocus value={name} onChange={(e) => setName(e.target.value)}
                     placeholder="e.g. AMC ladder 2370"
                     onKeyDown={(e) => { if (e.key === "Enter" && name.trim() && !busy) run(() => experimentAction("start", name, goal)) }} />
              <label className="text-[11px] uppercase tracking-wider text-muted-foreground">goal <span className="normal-case tracking-normal opacity-70">(optional)</span></label>
              <input className={inputCls} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="one line on what this is for" />
              {current && <ResetNote verb="Starting a new experiment" />}
              <div className="flex justify-end">
                <Button variant="primary" size="sm" disabled={busy || !name.trim()}
                        onClick={() => run(() => experimentAction("start", name, goal))}>
                  {current ? "Start and switch" : "Start"}
                </Button>
              </div>
            </TabsContent>
          </Tabs>

          {!gate && (
            <DialogPrimitive.Close asChild>
              <button className="absolute right-3 top-3 rounded px-1.5 text-muted-foreground hover:text-foreground" aria-label="Close">×</button>
            </DialogPrimitive.Close>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

function ResetNote({ verb }: { verb: string }) {
  return (
    <p className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[12px] text-warn">
      {verb} <strong>resets the session and the radios</strong>: the built structure, tuning and the agent's
      claims are cleared; the hop plan is dropped and the link returns to the declared channel; the
      interferer is disarmed if it was transmitting. The history of each experiment is kept. The
      exact list of what was cleared is shown afterwards.
    </p>
  )
}
