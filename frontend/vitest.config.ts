import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

// Separate from vite.config.ts: Vitest 5 no longer accepts a `test` key inside
// the Vite config. Tailwind is omitted here since tests do not assert on styles.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    // The default `forks` pool fails to hand off workers on Windows
    // ("Timeout waiting for worker to respond"); threads is reliable here.
    pool: 'threads',
  },
})
