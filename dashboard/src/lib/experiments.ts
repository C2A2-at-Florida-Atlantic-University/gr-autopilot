import { toast } from "sonner"
import { authHeaders } from "@/lib/operator"
import type { ExperimentAction, ResetReport } from "@/types"

/**
 * POST /experiments — start, switch, rename, delete. The first three are the same verbs the agent
 * has over MCP, so the dashboard and the model can never disagree about what an experiment is or
 * where it lives. `delete` is operator-only and has no MCP tool behind it: an agent that could
 * remove a previous run's measurements could destroy the evidence it exists to produce.
 *
 * A start or switch resets the session and the radios; the server says exactly what it cleared
 * and that report is shown verbatim, because "reset" without the list is a promise nobody can
 * check.
 */
export type Verb = "start" | "switch" | "rename" | "delete"

/** A page served to another machine: the daemon exempts only the bench host from its tokens. */
export function isRemotePage(): boolean {
  return !["127.0.0.1", "localhost", "[::1]", "::1"].includes(window.location.hostname)
}

export function describeReset(r: ResetReport | undefined): string {
  if (!r) return ""
  const lines = [...r.session, ...r.hardware, ...r.operator]
  return lines.length ? lines.join(" · ") : "nothing to clear"
}

export async function experimentAction(verb: Verb, name: string, goal?: string): Promise<ExperimentAction | null> {
  try {
    const r = await fetch("./experiments", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ action: verb, name, ...(goal !== undefined ? { goal } : {}) }),
    })
    const text = await r.text()
    let data: (ExperimentAction & { error?: string; code?: string }) | null = null
    try { data = JSON.parse(text) } catch { /* tolerate */ }
    if (r.status === 401) {
      // The daemon says which way the check failed (no header at all vs. a value that did not
      // match); show that, because "required" alone cannot be acted on by someone who believes
      // they have already entered it.
      toast.error("Access token required", {
        description: data?.error
          ?? "Off the bench host, enter the daemon's --token value.",
        duration: 12000,
      })
      return null
    }
    if (!r.ok) {
      toast.error(verb === "start" ? "Could not start the experiment"
        : verb === "switch" ? "Could not switch experiment"
        : verb === "delete" ? "Could not delete the experiment" : "Could not rename",
        { description: data?.error ?? `HTTP ${r.status}` })
      return null
    }
    const out = data as ExperimentAction
    if (out.action === "started" || out.action === "switched") {
      toast.success(`${out.action === "started" ? "Started" : "Switched to"} “${out.experiment.name}”`, {
        description: "Reset: " + describeReset(out.reset),
        duration: 9000,
      })
    } else if (out.action === "renamed") {
      toast.success(`Renamed to “${out.experiment.name}”`, { description: "Nothing was reset." })
    } else if (out.action === "deleted") {
      // Name what was destroyed, not that something was. The iteration count is the only measure
      // of what is gone, and it can no longer be looked up anywhere.
      toast.success(`Deleted “${out.experiment.name}”`, {
        description: `${out.experiment.iterations} iterations removed with ${out.experiment.dir}. `
          + "This cannot be undone.",
        duration: 9000,
      })
    }
    return out
  } catch {
    toast.error("Cannot reach the daemon", { description: "POST /experiments did not complete." })
    return null
  }
}
