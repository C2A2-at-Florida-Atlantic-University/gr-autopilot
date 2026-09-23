# gr-autopilot dashboard

The telemetry console the daemon serves at `/`. React 19 + TypeScript + Vite + Tailwind v4, with
Radix primitives in `src/components/ui/` following shadcn conventions. `dist/` is **committed on
purpose**: the bench serves the built bundle from disk with no Node toolchain.

## Build

```bash
npm install
npm run build          # tsc -b && vite build -> dist/ (what the daemon serves)
npm run lint           # oxlint over src/
npm run build:single   # optional: one self-contained HTML with a baked recording
```

No daemon restart is needed after a build — reload the page.

## Layout

- `src/App.tsx` — the shell: header (liveness, experiment name, provenance), left navigation rail
  with the pinned bench state, content, and the **history rail** on the right.
- `src/components/ExperimentDialog.tsx` — where an experiment is named, switched or renamed. Opens on
  its own (and cannot be dismissed) while the daemon has nothing selected; from the header chip
  otherwise. Start/switch show what will be reset before the button, and the toast repeats what was.
- `src/components/HistoryRail.tsx` — the edit ledger as a full-height, resizable (240–560 px),
  collapsible column; width and state are remembered per browser. Below 1280 px it is an overlay.
- `src/pages/Overview.tsx` — measurement tiles, constellation, spectrum, waterfall, trends. When
  nothing is running the last frame is kept, dimmed, under a banner stating its age.
- `src/components/{Constellation,Spectrum,Waterfall,Trends,Occupancy}.tsx` — canvas renderers.
  They take colour from the CSS tokens through `src/lib/ink.ts`; do not hard-code hex in them.
- `src/lib/axis.ts` — shared tick/format helpers so every axis names a quantity and a unit.
- `src/index.css` — the token layer (palette, type scale, space scale, trace ink, density). Dark is
  the default; `:root[data-theme="light"]` overrides the palette for the light theme.
- `src/components/ThemeToggle.tsx` — the light/dark switcher pinned to the header's top-right corner.
  The choice is stored per browser (`gra.theme`), and an inline script in `index.html` applies it
  before first paint. A change calls `resetInk()`, which makes every canvas repaint with the new ink.

## Conventions

- Fonts (IBM Plex Sans / Mono) are self-hosted via `@fontsource` so the console works offline.
- Add UI primitives by hand in `src/components/ui/`; do **not** run `shadcn init`, which rewrites
  `index.css` and flattens the instrument palette.
- The access token is held in memory only (`src/lib/operator.ts`); never persist it.
- Semantic colours (good / warn / bad) are never used for navigation; the accent is never used for
  a verdict.
- Every colour is a token with a value in both themes. Keep token values as hex: `ink.ts` builds
  canvas transparency from them.
