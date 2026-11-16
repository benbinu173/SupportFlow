import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

import { getTenant, json, loginWithRateLimit, settings, submitSignIn, verificationLink } from './helpers'
import type { components } from '../src/types/api'

test.use({ trace: 'off' })
test.describe.configure({ timeout: 180_000 })

test('live workspace signup waits for delivered email before creating a session', async ({ browser, request }, testInfo) => {
  const config = settings(testInfo)
  const suffix = randomUUID().slice(0, 8)
  const workspace = `Local ${config.production ? 'production' : 'live'} signup ${suffix}`
  const email = `signup-${suffix}@${config.domain}`
  const password = 'SignupBrowserPassw0rd!23'
  const context = await browser.newContext({ baseURL: config.baseURL, ignoreHTTPSErrors: Boolean(testInfo.project.use.ignoreHTTPSErrors) })
  const page = await context.newPage()
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error' && /content security policy|violates.*directive/i.test(message.text())) errors.push(message.text())
  })
  try {
    await page.goto('/register')
    await page.getByRole('textbox', { name: 'Organization' }).fill(workspace)
    await page.getByRole('textbox', { name: 'Your name' }).fill('Signup Verification Owner')
    await page.getByRole('textbox', { name: 'Work email' }).fill(email)
    await page.getByLabel(/^Password\s*\*?$/).fill(password)
    const registration = page.waitForResponse((response) => response.url() === `${config.baseURL}/api/v1/auth/register` && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Create workspace' }).click()
    const registered = await registration
    expect(registered.status()).toBe(201)
    expect(Object.keys(await registered.json()).sort()).toEqual(['detail', 'email'])
    expect(registered.headers()['set-cookie']).toBeUndefined()
    await expect(page).toHaveURL(/\/verify-email$/)
    await expect(page.getByRole('heading', { level: 1, name: 'Check your email' })).toBeVisible()
    expect((await context.cookies()).some((cookie) => cookie.name === 'sf_refresh')).toBe(false)
    const blocked = await loginWithRateLimit(request, config.apiURL, { email, password })
    expect(blocked.status()).toBe(403)
    expect((await blocked.json()).error.code).toBe('EMAIL_NOT_VERIFIED')

    const link = await verificationLink(request, config, email, workspace)
    expect(new URL(link).origin).toBe(new URL(config.baseURL).origin)
    expect(new URL(link).search).toBe('')
    await page.goto(link).catch(() => { throw new Error('The delivered signup verification link could not be opened') })
    await expect(page.getByRole('button', { name: 'Verify email' })).toBeVisible()
    await expect.poll(() => new URL(page.url()).hash).toBe('')
    const stillBlocked = await loginWithRateLimit(request, config.apiURL, { email, password })
    expect(stillBlocked.status()).toBe(403)
    const confirmation = page.waitForResponse((response) => response.url() === `${config.baseURL}/api/v1/auth/verify-email` && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Verify email' }).click()
    const confirmed = await confirmation
    expect(confirmed.status()).toBe(200)
    const verified = await confirmed.json() as components['schemas']['VerifyEmailResponse']
    await expect(page.getByRole('heading', { level: 1, name: 'Email verified' })).toBeVisible()
    expect((await context.cookies()).some((cookie) => cookie.name === 'sf_refresh')).toBe(false)
    await page.getByRole('button', { name: 'Continue to sign in' }).click()
    await expect(page.getByRole('textbox', { name: 'Email', exact: true })).toHaveValue(email)
    await page.getByLabel(/^Password\s*\*?$/).fill(password)
    const signedIn = await submitSignIn(page, testInfo)
    expect(signedIn.status()).toBe(200)
    expect(signedIn.request().postDataJSON().organization_slug).toBe(verified.organization_slug)
    await expect(page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
    await page.reload()
    await expect(page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
    const cookie = (await context.cookies()).find((item) => item.name === 'sf_refresh')
    expect(cookie && { secure: cookie.secure, httpOnly: cookie.httpOnly, sameSite: cookie.sameSite })
      .toEqual({ secure: config.production, httpOnly: true, sameSite: 'Lax' })
    await page.screenshot({ path: testInfo.outputPath('signup-verified-desktop.png'), fullPage: true, animations: 'disabled' })
    await page.getByRole('button', { name: 'Account menu', exact: true }).click()
    await page.getByRole('menuitem', { name: 'Sign out', exact: true }).click()
    await expect(page).toHaveURL(/\/login$/)
    expect((await context.cookies()).some((item) => item.name === 'sf_refresh')).toBe(false)
    await page.goto(link).catch(() => { throw new Error('The reused signup verification link could not be opened') })
    await page.getByRole('button', { name: 'Verify email' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Request a new verification link' })).toBeVisible()
    expect(errors).toEqual([])
    await testInfo.attach('signup-verification-record', { contentType: 'application/json', body: Buffer.from(JSON.stringify({ workspace: verified.organization_slug, registration_status: registered.status(), no_session_before_sign_in: true, blocked_before_confirmation: true, delivered_link_origin: new URL(link).origin, explicit_confirmation: true, sign_in_and_reload: true, replay_refused: true, browser_errors: errors.length })) })
  } finally {
    await context.close()
    // Keep this uniquely named test workspace and its email/audit history for inspection.
  }
})

test('live email confirmation enables sign-in and cannot be replayed', async ({ browser, request }, testInfo) => {
  const config = settings(testInfo)
  const { headers } = await getTenant(request, testInfo)
  const password = 'VerificationBrowserPassw0rd!23'
  const user = await json<components['schemas']['UserRead']>(await request.post(`${config.apiURL}/users`, {
    headers,
    data: { name: 'Email Verification Check', email: `email-verification-${randomUUID().slice(0, 8)}@${config.domain}`, password, role: 'agent' },
  }))
  const context = await browser.newContext({ baseURL: config.baseURL, ignoreHTTPSErrors: Boolean(testInfo.project.use.ignoreHTTPSErrors), viewport: { width: 1366, height: 768 } })
  const page: Page = await context.newPage()
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  try {
    expect(user.email_verified_at).toBeNull()
    const blocked = await loginWithRateLimit(request, config.apiURL, { email: user.email, password })
    expect(blocked.status()).toBe(403)
    expect((await blocked.json()).error.code).toBe('EMAIL_NOT_VERIFIED')
    expect(blocked.headers()['set-cookie']).toBeUndefined()

    await page.goto('/login')
    await page.getByRole('textbox', { name: 'Email', exact: true }).fill(user.email)
    await page.getByLabel(/^Password\s*\*?$/).fill(password)
    await submitSignIn(page, testInfo)
    await expect(page).toHaveURL(/\/verify-email$/)
    await expect(page.getByRole('textbox', { name: 'Email', exact: true })).toHaveValue(user.email)
    const resend = page.waitForResponse((response) => response.url() === `${config.baseURL}/api/v1/auth/resend-verification` && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Send verification link' }).click()
    expect((await resend).status()).toBe(202)
    await expect(page.getByRole('heading', { level: 1, name: 'Check your email' })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('verification-waiting-desktop.png'), fullPage: true, animations: 'disabled' })

    const workspace = config.production ? 'Local production browser checks' : 'Local live browser checks'
    const link = await verificationLink(request, config, user.email, workspace)
    const url = new URL(link)
    expect(url.origin).toBe(new URL(config.baseURL).origin)
    expect(url.pathname).toBe('/verify-email')
    expect(url.search).toBe('')
    const token = new URLSearchParams(url.hash.slice(1)).get('token')
    if (!token) throw new Error('The email did not contain a fragment credential')
    const outgoingDocuments: string[] = []
    page.on('request', (outgoing) => { if (outgoing.resourceType() === 'document') outgoingDocuments.push(outgoing.url()) })
    await page.goto(link).catch(() => { throw new Error('The delivered verification link could not be opened') })
    await expect(page.getByRole('button', { name: 'Verify email' })).toBeVisible()
    await expect.poll(() => new URL(page.url()).hash).toBe('')
    expect(outgoingDocuments.every((address) => !address.includes(token))).toBe(true)
    const stillPending = await json<components['schemas']['UserRead']>(await request.get(`${config.apiURL}/users/${user.id}`, { headers }))
    expect(stillPending.email_verified_at).toBeNull()
    await page.screenshot({ path: testInfo.outputPath('verification-confirm-desktop.png'), fullPage: true, animations: 'disabled' })
    await page.setViewportSize({ width: 390, height: 844 })
    await expect(page.getByRole('button', { name: 'Verify email' })).toBeInViewport()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('verification-confirm-mobile.png'), fullPage: true, animations: 'disabled' })
    await page.getByRole('button', { name: 'Verify email' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Email verified' })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('verification-success-mobile.png'), fullPage: true, animations: 'disabled' })
    expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 })
    const replay = await request.post(`${config.apiURL}/auth/verify-email`, { data: { token } })
    expect(replay.status()).toBe(400)
    expect((await replay.json()).error.code).toBe('INVALID_VERIFICATION_TOKEN')
    await page.getByRole('button', { name: 'Continue to sign in' }).click()
    await expect(page.getByRole('textbox', { name: 'Email', exact: true })).toHaveValue(user.email)
    await page.getByLabel(/^Password\s*\*?$/).fill(password)
    await submitSignIn(page, testInfo)
    await expect(page.getByRole('heading', { name: 'Welcome back, Email', exact: true })).toBeVisible()
    // The connection badge is hidden on mobile; first assert mobile sign-in, then live transport.
    await page.setViewportSize({ width: 1366, height: 768 })
    await expect(page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
    await page.reload()
    await expect(page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
    const verified = await json<components['schemas']['UserRead']>(await request.get(`${config.apiURL}/users/${user.id}`, { headers }))
    expect(verified.email_verified_at).not.toBeNull()
    expect(errors).toEqual([])
    await testInfo.attach('verification-record', { contentType: 'application/json', body: Buffer.from(JSON.stringify({ user_id: user.id, verified: true, blocked_before_confirmation: true, replay_status: replay.status(), cookie_absent_before_verification: true, browser_errors: errors.length })) })
  } finally {
    await context.close()
    await json(await request.post(`${config.apiURL}/users/${user.id}/deactivate`, { headers }))
  }
})
