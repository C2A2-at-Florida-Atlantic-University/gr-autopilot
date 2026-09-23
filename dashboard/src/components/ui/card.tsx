import type { ComponentProps } from "react"
import { cn } from "@/lib/utils"

// Three visual tiers so the eye lands on the right thing (design review #5):
//  primary   — key readouts (plots, health): elevated, a hairline inset accent
//  secondary — the default gradient card (the agent narrative, controls)
//  tertiary  — log/secondary surfaces (ledger, occupancy): flat, recessed
type Tier = "primary" | "secondary" | "tertiary"

const TIER: Record<Tier, string> = {
  primary:
    "bg-gradient-to-b from-card to-muted border-border-strong " +
    "shadow-[0_16px_40px_-28px_var(--shadow)] ring-1 ring-inset ring-white/[0.02]",
  secondary: "bg-gradient-to-b from-card to-muted",
  tertiary: "bg-muted/60",
}

function Card({
  className,
  tier = "secondary",
  ...props
}: ComponentProps<"div"> & { tier?: Tier }) {
  return (
    <div
      data-slot="card"
      data-tier={tier}
      className={cn("text-card-foreground flex flex-col rounded-xl border", TIER[tier], className)}
      {...props}
    />
  )
}

function CardContent({ className, ...props }: ComponentProps<"div">) {
  return <div data-slot="card-content" className={cn("p-4", className)} {...props} />
}

export { Card, CardContent }
