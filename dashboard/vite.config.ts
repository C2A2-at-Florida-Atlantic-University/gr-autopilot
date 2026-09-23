import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { viteSingleFile } from 'vite-plugin-singlefile'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

// `vite build --mode singlefile` inlines everything into one CSP-safe HTML and bakes in the
// recorded narrative (window global __GRA_REC__) for the shareable artifact. The default build
// leaves __GRA_REC__ null, so the app polls /data live (localhost, served by the stdlib server).
export default defineConfig(({ mode }) => {
  const single = mode === 'singlefile'
  return {
    base: './',
    plugins: [react(), tailwindcss(), ...(single ? [viteSingleFile()] : [])],
    resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
    define: {
      __GRA_REC__: single ? readFileSync('./recording.json', 'utf8') : 'null',
    },
    build: { outDir: single ? 'dist-single' : 'dist', emptyOutDir: true },
  }
})
