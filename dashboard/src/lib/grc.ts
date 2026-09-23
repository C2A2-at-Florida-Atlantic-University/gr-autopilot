export interface Block { name: string; id: string }
export type Conn = [string, string, string, string]

/** Enough of a GRC file to draw it: block names/ids under `blocks:`, and `connections:` rows. The
 *  file is YAML; this reads the two list shapes GRC 3.10 writes rather than shipping a YAML parser. */
export function parseGrc(text: string): { blocks: Block[]; conns: Conn[]; options: Record<string, string> } {
  const blocks: Block[] = [], conns: Conn[] = [], options: Record<string, string> = {}
  let section = "", cur: Block | null = null, inOptParams = false
  for (const raw of text.split("\n")) {
    const line = raw.replace(/\r$/, "")
    if (/^blocks:/.test(line)) { section = "blocks"; continue }
    if (/^connections:/.test(line)) { section = "conns"; continue }
    if (/^options:/.test(line)) { section = "options"; continue }
    if (/^[a-z_]+:/.test(line)) { section = ""; continue }
    if (section === "blocks") {
      const m = line.match(/^- name: (.+)$/)
      if (m) { cur = { name: m[1].trim(), id: "" }; blocks.push(cur); continue }
      const mi = line.match(/^ {2}id: (.+)$/)
      if (mi && cur) cur.id = mi[1].trim()
    } else if (section === "conns") {
      const m = line.match(/^- \[(.+)\]$/)
      if (m) {
        const parts = m[1].split(",").map((s) => s.trim().replace(/^'(.*)'$/, "$1"))
        if (parts.length >= 4) conns.push([parts[0], parts[1], parts[2], parts[3]])
      }
    } else if (section === "options") {
      if (/^ {2}parameters:/.test(line)) { inOptParams = true; continue }
      const m = inOptParams ? line.match(/^ {4}([a-z_]+): (.+)$/) : null
      if (m) options[m[1]] = m[2].replace(/^'(.*)'$/, "$1")
      else if (/^ {2}[a-z_]+:/.test(line)) inOptParams = false
    }
  }
  return { blocks, conns, options }
}
