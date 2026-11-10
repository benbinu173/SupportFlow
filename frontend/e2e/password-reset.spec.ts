import { expect, test } from '@playwright/test'

const token = 'browser-email-recovery-token'
const password = 'a-new-workspace-password'

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/refresh', (route) => route.fulfill({ status: 401, json: { error: { code: 'AUTHENTICATION_REQUIRED', message: 'Authentication required.' } } }))
})

test('sign-in links to recovery and confirms an email request without disclosing an account', async ({ page }) => {
  let submitted: unknown
  await page.route('**/api/v1/auth/forgot-password', (route) => {
    submitted = route.request().postDataJSON()
    return route.fulfill({ status: 202, json: { detail: "If an active account matches that email, we'll send a password reset link." } })
  })
  await page.goto('/login')
  await page.getByRole('link', { name: 'Forgot password?' }).click()
  await page.getByRole('textbox', { name: 'Email' }).fill('unknown@example.test')
  await page.getByRole('button', { name: 'Send reset link' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Check your email' })).toBeVisible()
  await expect(page.getByText(/If an active account matches/)).toBeVisible()
  expect(submitted).toEqual({ email: 'unknown@example.test' })
})

test('a fragment link submits matching passwords and returns to sign in', async ({ page }) => {
  let submitted: unknown
  await page.route('**/api/v1/auth/reset-password', (route) => {
    submitted = route.request().postDataJSON()
    return route.fulfill({ json: { detail: 'Password updated. Sign in with your new password.' } })
  })
  await page.goto(`/reset-password#token=${token}`)
  await expect(page).toHaveURL(/\/reset-password$/)
  await page.getByLabel('New password', { exact: false }).filter({ visible: true }).first().fill(password)
  await page.getByLabel('Confirm password', { exact: false }).filter({ visible: true }).first().fill(password)
  await page.getByRole('button', { name: 'Update password' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Password updated' })).toBeVisible()
  expect(submitted).toEqual({ token, password })
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 })
  await page.getByRole('link', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
})

test('an expired link offers a new request', async ({ page }) => {
  await page.route('**/api/v1/auth/reset-password', (route) => route.fulfill({ status: 400, json: { error: { code: 'INVALID_RESET_TOKEN', message: 'This reset link is invalid or has expired. Request a new one.' } } }))
  await page.goto(`/reset-password#token=${token}`)
  await page.getByLabel('New password', { exact: false }).first().fill(password)
  await page.getByLabel('Confirm password', { exact: false }).first().fill(password)
  await page.getByRole('button', { name: 'Update password' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Request a new reset link' })).toBeVisible()
  await page.getByRole('link', { name: 'Request reset link' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Forgot your password?' })).toBeVisible()
})

test('recovery stays readable on a phone and explains a missing credential', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/reset-password')
  await expect(page.getByRole('heading', { level: 1, name: 'Request a new reset link' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Request reset link' })).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
