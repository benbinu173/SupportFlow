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
    // Pre-bundle dependencies with esbuild instead of leaving Vitest's module
    // runner to resolve them file by file at run time.
    //
    // This exists for one package. `@phosphor-icons/react` is published as
    // hundreds of individual ES modules — one file per icon — and any test that
    // imports a page transitively imports most of them. Two workers therefore
    // means two thousand-odd file handles, which on Windows produces two
    // failures that look nothing like each other and have the same cause:
    // `EMFILE: too many open files`, and workers that time out before they
    // start. Both report the *file* as failed with zero failed assertions, so a
    // run can print "11 passed (11)" and still exit non-zero with two files
    // never executed — a green result that is not one.
    //
    // Bundling turns that graph into a single artifact per worker. It is a test
    // environment concern only; the application build is untouched.
    deps: {
      optimizer: {
        web: {
          enabled: true,
        },
      },
    },
    // Two, and not Vitest's default of roughly one worker per core. Every
    // worker already carries its own jsdom, and the icon bundle above is
    // duplicated per worker too, so the handle budget rather than the CPU is
    // what sets the ceiling. Two costs nothing — the suite runs in about twenty
    // seconds — and it is what makes a many-core developer machine behave like
    // a CI runner.
    maxWorkers: 2,
  },
})
