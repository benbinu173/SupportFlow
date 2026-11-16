import { defineConfig, devices } from '@playwright/test'

/** The same production workflows, against the isolated memory-limited preview. */
export default defineConfig({
  testDir: './e2e-live',
  workers: 1,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: 'playwright-report/preview', open: 'never' }]],
  outputDir: 'test-results/preview',
  use: { trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [{
    name: 'preview',
    use: {
      ...devices['Desktop Chrome'],
      baseURL: 'https://localhost:8443',
      ignoreHTTPSErrors: true,
    },
  }],
})
