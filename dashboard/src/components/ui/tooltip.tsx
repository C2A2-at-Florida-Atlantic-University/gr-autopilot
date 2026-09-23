import type { ComponentProps, ReactNode } from "react"
import * as TooltipPrimitive from "@radix-ui/react-tooltip"
import { cn } from "@/lib/utils"

const TooltipProvider = TooltipPrimitive.Provider
const Tooltip = TooltipPrimitive.Root
const TooltipTrigger = TooltipPrimitive.Trigger

function TooltipContent({ className, sideOffset = 6, ...props }: ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        sideOffset={sideOffset}
        className={cn(
          "z-50 max-w-[38ch] rounded-md border bg-card px-2.5 py-1.5 text-[12px] leading-snug",
          "text-foreground shadow-[0_10px_30px_-12px_var(--shadow)]",
          className,
        )}
        {...props}
      />
    </TooltipPrimitive.Portal>
  )
}

/** The common case: a dotted-underlined term that explains itself. Terms of art (EVM, Es/N0,
 *  detect margin) should never appear on this console unexplained. */
function Term({ children, hint }: { children: ReactNode; hint: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className="cursor-help underline decoration-dotted underline-offset-2 outline-none focus-visible:ring-2 focus-visible:ring-primary/60">
          {children}
        </span>
      </TooltipTrigger>
      <TooltipContent>{hint}</TooltipContent>
    </Tooltip>
  )
}

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider, Term }
