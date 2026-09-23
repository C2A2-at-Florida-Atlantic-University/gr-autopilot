import type { ComponentProps } from "react"
import * as SwitchPrimitive from "@radix-ui/react-switch"
import { cn } from "@/lib/utils"

function Switch({ className, tone = "default", ...props }:
  ComponentProps<typeof SwitchPrimitive.Root> & { tone?: "default" | "danger" }) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        "peer inline-flex h-[18px] w-[32px] shrink-0 items-center rounded-full border transition-colors",
        "outline-none focus-visible:ring-2 focus-visible:ring-primary/60 disabled:opacity-45",
        "data-[state=unchecked]:border-border data-[state=unchecked]:bg-muted",
        tone === "danger"
          ? "data-[state=checked]:border-bad/60 data-[state=checked]:bg-bad/30"
          : "data-[state=checked]:border-primary/60 data-[state=checked]:bg-primary/30",
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          "pointer-events-none block h-3 w-3 rounded-full bg-foreground shadow transition-transform",
          "translate-x-[3px] data-[state=checked]:translate-x-[16px]",
          tone === "danger" && "data-[state=checked]:bg-bad",
        )}
      />
    </SwitchPrimitive.Root>
  )
}
export { Switch }
