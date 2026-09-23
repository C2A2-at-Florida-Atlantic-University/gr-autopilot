import { Badge } from "@/components/ui/badge"
import type { Telemetry } from "@/lib/telemetry"

type Tone = "good" | "bad" | "warn" | "default"

function toneOf(v: string): Tone {
  if (v === "kept" || v === "clean") return "good"
  if (v === "reverted" || v === "jammed") return "bad"
  if (v === "tuning") return "warn"
  return "default"
}

function phaseName(v: string, loop: string): string {
  const map: Record<string, string> = {
    tuning: loop === "inner" ? "Inner loop" : "Ladder",
    kept: "Selected",
    reverted: "Interference",
    jammed: "Scanning",
    clean: "Channel found",
  }
  return map[v] ?? "Run"
}

export function Transport({ tele }: { tele: Telemetry }) {
  const fr = tele.frames[tele.idx]
  const last = fr.ledger[fr.ledger.length - 1]
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
      <button
        onClick={tele.toggle}
        aria-label="Play or pause"
        className="min-w-[46px] rounded-lg border bg-card px-3 py-2 font-mono text-xs tracking-widest hover:border-primary hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
      >
        {tele.playing ? "❚❚" : "▶"}
      </button>
      <div className="flex min-w-[180px] flex-1 gap-1.5">
        {tele.frames.map((_, j) => (
          <button
            key={j}
            aria-label={`Go to step ${j + 1}`}
            onClick={() => tele.goTo(j)}
            className={
              "h-1.5 flex-1 rounded-full transition-colors " +
              (j < tele.idx
                ? "bg-primary/55"
                : j === tele.idx
                  ? "bg-primary shadow-[0_0_8px_var(--primary)]"
                  : "bg-border")
            }
          />
        ))}
      </div>
      <div className="flex basis-full items-center gap-2.5 text-sm">
        {last ? (
          <>
            <Badge variant={toneOf(last.verdict)} className="rounded-md px-2 py-0.5 text-[10px] uppercase tracking-widest">
              {phaseName(last.verdict, fr.snapshot.active_loop.loop)}
            </Badge>
            <span>{last.edit_description}</span>
          </>
        ) : (
          <span className="text-muted-foreground">no edits yet</span>
        )}
      </div>
    </div>
  )
}
