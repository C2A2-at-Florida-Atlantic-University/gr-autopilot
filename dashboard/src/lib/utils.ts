import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function fmtBER(x: number | null | undefined): string {
  if (x == null) return "–"
  if (x === 0) return "0"
  return x < 1e-2 ? x.toExponential(1) : x.toFixed(3)
}

export function fmtAge(s: number | null) {
  if (s === null) return ""
  if (s < 90) return `${Math.round(s)}s`
  if (s < 5400) return `${Math.round(s / 60)}m`
  return `${Math.round(s / 3600)}h`
}
