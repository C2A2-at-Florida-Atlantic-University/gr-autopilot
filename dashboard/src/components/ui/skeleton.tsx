import type { ComponentProps } from "react"
import { cn } from "@/lib/utils"

/** Shaped like the thing that is arriving, not a spinner. */
function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("animate-pulse rounded-md bg-muted", className)} {...props} />
}
export { Skeleton }
