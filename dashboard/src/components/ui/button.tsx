import type { ComponentProps } from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-1.5 rounded-md border font-medium " +
    "transition-colors outline-none disabled:pointer-events-none disabled:opacity-45 " +
    "focus-visible:ring-2 focus-visible:ring-primary/60 focus-visible:ring-offset-1 " +
    "focus-visible:ring-offset-background [&_svg]:pointer-events-none",
  {
    variants: {
      variant: {
        default: "border-border bg-muted text-foreground hover:bg-border/60",
        primary: "border-primary/40 bg-primary/10 text-primary hover:bg-primary/20",
        // Destructive here means "this transmits" — the operator surface, nothing else.
        danger: "border-bad/45 bg-bad/10 text-bad hover:bg-bad/20",
        ghost: "border-transparent bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground",
        outline: "border-border bg-transparent text-foreground hover:bg-muted",
      },
      size: {
        sm: "h-7 px-2.5 text-[12px]",
        md: "h-8 px-3 text-[13px]",
        icon: "h-7 w-7 p-0",
      },
    },
    defaultVariants: { variant: "default", size: "md" },
  },
)

function Button({
  className, variant, size, asChild = false, ...props
}: ComponentProps<"button"> & VariantProps<typeof buttonVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : "button"
  return <Comp data-slot="button" className={cn(buttonVariants({ variant, size }), className)} {...props} />
}

export { Button, buttonVariants }
