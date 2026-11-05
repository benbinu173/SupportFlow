import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { configDefaults, defineConfig } from 'vitest/config'

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
    // Playwright uses the same `.spec.ts` convention as Vitest, but its browser
    // fixtures cannot run inside the jsdom unit-test pool.
    exclude: [...configDefaults.exclude, 'e2e/**'],
    // Form-flow tests exercise long accessible labels and policy feedback in
    // jsdom; fifteen seconds leaves room for slower Windows CI workers.
    testTimeout: 15_000,
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    // The default `forks` pool fails to hand off workers on Windows
    // ("Timeout waiting for worker to respond"); threads is reliable here.
    pool: 'threads',
    // Pre-bundle dependencies instead of leaving Vitest's module
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
        // Vitest 5 uses `client` for jsdom. The old `web` key is ignored, so
        // enabling it did not actually bundle the icon package.
        client: {
          enabled: true,
          include: ['@phosphor-icons/react'],
        },
      },
    },
    // Run one file at a time. Every worker carries its own jsdom and the icon
    // bundle above; parallel startup can exceed Windows' worker handshake
    // timeout before assertions begin. Serial execution is slower but stable.
    fileParallelism: false,
    maxWorkers: 1,
  },
})
