import { expect, test } from './fixtures'

const token = 'browser-verification-token'
const email = 'pending@example.test'

test('signup waits for verification and can resend the email', async ({ page }) => {
  await page.route('**/api/v1/auth/register', (route) => route.fulfill({ status: 201, json: { email, detail: 'Check your email.' } }))
  let requested: unknown
  await page.route('**/api/v1/auth/resend-verification', async (route) => {
    requested = route.request().postDataJSON()
    await route.fulfill({ status: 202, json: { detail: 'If an active account still needs verification...' } })
  })
  await page.goto('/register')
  await page.getByRole('textbox', { name: 'Organization' }).fill('Browser workspace')
  await page.getByRole('textbox', { name: 'Your name' }).fill('Browser Owner')
  await page.getByRole('textbox', { name: 'Work email' }).fill(email)
  await page.getByLabel(/^Password\s*\*?$/).fill('a-workspace-password')
  await page.getByRole('button', { name: 'Create workspace' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Check your email' })).toBeVisible()
  await expect(page).toHaveURL(/\/verify-email$/)
  await page.getByRole('button', { name: 'Resend verification email' }).click()
  expect(requested).toEqual({ email })
  await expect(page.getByRole('status')).toContainText(email)
})

test('a fragment link confirms only after clicking and then opens sign in', async ({ page }) => {
  let submitted: unknown
  await page.route('**/api/v1/auth/verify-email', async (route) => {
    submitted = route.request().postDataJSON()
    await route.fulfill({ json: { email, organization_slug: 'browser-workspace', detail: 'Email verified.' } })
  })
  await page.goto(`/verify-email#token=${token}`)
  await expect(page).toHaveURL(/\/verify-email$/)
  await expect(page.getByRole('button', { name: 'Verify email' })).toBeVisible()
  expect(submitted).toBeUndefined()
  await page.getByRole('button', { name: 'Verify email' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Email verified' })).toBeVisible()
  expect(submitted).toEqual({ token })
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 })
  await page.getByRole('button', { name: 'Continue to sign in' }).click()
  await expect(page.getByRole('textbox', { name: 'Email' })).toHaveValue(email)
})

test('an expired verification link offers a new email request', async ({ page }) => {
  await page.route('**/api/v1/auth/verify-email', (route) => route.fulfill({ status: 400, json: { error: { code: 'INVALID_VERIFICATION_TOKEN', message: 'This verification link is invalid or has expired.' } } }))
  await page.goto(`/verify-email#token=${token}`)
  await page.getByRole('button', { name: 'Verify email' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Request a new verification link' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Send verification link' })).toBeVisible()
})

test('verification and resend remain readable on a phone', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/verify-email#token=${token}`)
  await expect(page.getByRole('button', { name: 'Verify email' })).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.goto('/verify-email')
  await expect(page.getByRole('button', { name: 'Send verification link' })).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
