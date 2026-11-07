import { defineConfig, devices } from '@playwright/test'

/** Checks the existing local Docker stack through Caddy, without a Vite server. */
export default defineConfig({
  testDir: './e2e-live',
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: 'playwright-report/production', open: 'never' }]],
  outputDir: 'test-results/production',
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{
    name: 'production',
    use: {
      ...devices['Desktop Chrome'],
      baseURL: 'https://localhost',
      // Browser acceptance of Caddy's local CA; deploy_smoke separately verifies its chain.
      ignoreHTTPSErrors: true,
    },
  }],
})
