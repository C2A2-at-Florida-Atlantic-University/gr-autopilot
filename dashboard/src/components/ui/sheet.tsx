import type { ComponentProps } from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { cn } from "@/lib/utils"

const Sheet = DialogPrimitive.Root
const SheetTrigger = DialogPrimitive.Trigger
const SheetClose = DialogPrimitive.Close
const SheetTitle = DialogPrimitive.Title
const SheetDescription = DialogPrimitive.Description

function SheetContent({ className, children, side = "right", ...props }:
  ComponentProps<typeof DialogPrimitive.Content> & { side?: "left" | "right" }) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-[var(--overlay)]" />
      <DialogPrimitive.Content
        className={cn(
          "fixed top-0 z-50 flex h-dvh w-[86vw] max-w-[380px] flex-col gap-3 border-border bg-background p-4",
          side === "right" ? "right-0 border-l" : "left-0 border-r",
          className,
        )}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  )
}

/** Confirm before anything that transmits (design language L4). Names the frequency and the
 *  instrument in the body, because "are you sure?" is not a description of what will happen. */
function ConfirmDialog({ open, onOpenChange, title, body, confirmLabel, onConfirm }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  title: string
  body: React.ReactNode
  confirmLabel: string
  onConfirm: () => void
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-[var(--overlay)]" />
        <DialogPrimitive.Content
          className="fixed left-1/2 top-1/2 z-50 w-[min(92vw,460px)] -translate-x-1/2 -translate-y-1/2
                     rounded-xl border border-bad/40 bg-card p-5 shadow-[0_24px_60px_-24px_var(--shadow)]"
        >
          <DialogPrimitive.Title className="font-mono text-[13px] font-semibold text-bad">
            {title}
          </DialogPrimitive.Title>
          <DialogPrimitive.Description className="mt-2 text-[13px] leading-relaxed text-muted-foreground">
            {body}
          </DialogPrimitive.Description>
          <div className="mt-4 flex justify-end gap-2">
            <DialogPrimitive.Close asChild>
              <button className="h-8 rounded-md border border-border bg-muted px-3 text-[13px] hover:bg-border/60">
                Cancel
              </button>
            </DialogPrimitive.Close>
            <button
              onClick={() => { onOpenChange(false); onConfirm() }}
              className="h-8 rounded-md border border-bad/50 bg-bad/15 px-3 text-[13px] font-medium text-bad hover:bg-bad/25"
            >
              {confirmLabel}
            </button>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

export { Sheet, SheetTrigger, SheetClose, SheetContent, SheetTitle, SheetDescription, ConfirmDialog }
