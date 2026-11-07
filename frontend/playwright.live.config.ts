import { defineConfig, devices } from '@playwright/test'

/** Local integration checks require the API, Redis, Postgres, worker, and Mailpit. */
export default defineConfig({
  testDir: './e2e-live',
  testIgnore: '**/production.spec.ts',
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: 'playwright-report/live', open: 'never' }]],
  outputDir: 'test-results/live',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:4174',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    // Vite preview inherits the /api, /health and /ws proxies from vite.config.ts.
    command: 'npm exec vite build && npm run preview -- --host 127.0.0.1 --port 4174 --strictPort',
    url: 'http://127.0.0.1:4174',
    reuseExistingServer: false,
    timeout: 180_000,
  },
})
