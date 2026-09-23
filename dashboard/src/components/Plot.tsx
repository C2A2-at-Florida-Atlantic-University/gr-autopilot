import type { ReactNode } from "react"
import { Card } from "@/components/ui/card"

/** A titled plot card. Plots are key readouts, so they sit in the primary (elevated) tier. */
export function Plot({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card tier="primary" className="flex h-full flex-col px-3.5 pb-3 pt-3">
      <figcaption className="mb-2 text-[10px] uppercase tracking-wider text-muted-foreground">
        {title}
      </figcaption>
      <div className="min-h-[180px] flex-1">{children}</div>
    </Card>
  )
}
