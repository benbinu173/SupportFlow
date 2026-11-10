import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

import { getTenant, json, settings, signIn } from './helpers'
import type { components } from '../src/types/api'

type User = components['schemas']['UserRead']
type Token = components['schemas']['TokenResponse']
type Mail = { ID: string; Subject: string; To: Array<{ Address: string }> }

// Recovery credentials stay in memory; traces would retain email and request bodies.
test.use({ trace: 'off' })

test('live recovery email resets the account and revokes previous sessions', async ({ browser, request }, testInfo) => {
  const config = settings(testInfo)
  const { headers } = await getTenant(request, testInfo)
  const oldPassword = 'LocalBrowserPassw0rd!23'
  const newPassword = 'RecoveredBrowserPassw0rd!24'
  const user = await json<User>(await request.post(`${config.apiURL}/users`, {
    headers,
    data: {
      name: 'Password Recovery Browser Check',
      email: `password-reset-${randomUUID().slice(0, 8)}@${config.domain}`,
      password: oldPassword,
      role: 'agent',
    },
  }))
  const pages: Page[] = []
  const errors: string[] = []
  try {
    const oldLogin = await request.post(`${config.apiURL}/auth/login`, {
      data: { email: user.email, password: oldPassword },
    })
    const oldToken = await json<Token>(oldLogin)
    const oldCookie = oldLogin.headers()['set-cookie']?.split(';', 1)[0]
    if (!oldCookie) throw new Error('Login did not issue a refresh cookie')

    const previousSession = await signIn(browser, { user, password: oldPassword }, testInfo, errors)
    pages.push(previousSession)
    const recovery = await signIn(browser, { user, password: oldPassword }, testInfo, errors)
    pages.push(recovery)

    // Recovery remains available to visitors who already have a browser session.
    await recovery.goto('/forgot-password')
    await expect(recovery.getByRole('heading', { level: 1, name: 'Forgot your password?' })).toBeVisible()
    await recovery.screenshot({ path: testInfo.outputPath('forgot-password-desktop.png'), fullPage: true, animations: 'disabled' })
    await recovery.getByRole('textbox', { name: 'Email' }).fill(user.email)
    await recovery.getByRole('button', { name: 'Send reset link' }).click()
    await expect(recovery.getByRole('heading', { level: 1, name: 'Check your email' })).toBeVisible()

    let delivered: Mail | undefined
    await expect.poll(async () => {
      const inbox = await json<{ messages: Mail[] }>(await request.get(`${config.mailpitURL}/api/v1/search`, {
        params: { query: `to:${user.email}` },
      }))
      delivered = inbox.messages.find((mail) =>
        mail.Subject.includes('Reset your password') && mail.To.some((recipient) => recipient.Address === user.email),
      )
      return Boolean(delivered)
    }, { timeout: 30_000, message: 'The email worker delivers the requested reset link to Mailpit' }).toBe(true)
    const body = await json<{ Text: string }>(await request.get(`${config.mailpitURL}/api/v1/message/${delivered!.ID}`))
    const link = body.Text.match(/https?:\/\/[^\s]+\/reset-password#token=[^\s]+/)?.[0]
    if (!link) throw new Error('The reset email did not include a recovery link')
    const resetURL = new URL(link)
    expect(resetURL.origin).toBe(new URL(config.baseURL).origin)
    expect(resetURL.pathname).toBe('/reset-password')
    expect(resetURL.search).toBe('')
    const token = new URLSearchParams(resetURL.hash.slice(1)).get('token')
    if (!token) throw new Error('The reset email did not include a fragment credential')

    const documentRequests: string[] = []
    recovery.on('request', (outgoing) => {
      if (outgoing.resourceType() === 'document') documentRequests.push(outgoing.url())
    })
    await recovery.goto(link).catch(() => { throw new Error('The delivered recovery link could not be opened') })
    await expect(recovery.getByRole('heading', { level: 1, name: 'Set a new password' })).toBeVisible()
    await expect.poll(() => new URL(recovery.url()).hash === '', {
      message: 'The recovery page removes the credential from the address',
    }).toBe(true)
    expect(documentRequests.every((url) => !url.includes(token))).toBe(true)
    await recovery.screenshot({ path: testInfo.outputPath('reset-password-desktop.png'), fullPage: true, animations: 'disabled' })
    await recovery.setViewportSize({ width: 390, height: 844 })
    await expect(recovery.getByRole('button', { name: 'Update password' })).toBeInViewport()
    expect(await recovery.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await recovery.screenshot({ path: testInfo.outputPath('reset-password-mobile.png'), fullPage: true, animations: 'disabled' })
    await recovery.getByLabel(/^New password/).first().fill(newPassword)
    await recovery.getByLabel(/^Confirm password/).first().fill(newPassword)
    await recovery.getByRole('button', { name: 'Update password' }).click()
    await expect(recovery.getByRole('heading', { level: 1, name: 'Password updated' })).toBeVisible()
    await expect(previousSession.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
    expect(await recovery.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })))
      .toEqual({ local: 0, session: 0 })

    const staleAccess = await request.get(`${config.apiURL}/auth/me`, {
      headers: { Authorization: `Bearer ${oldToken.access_token}` },
    })
    expect(staleAccess.status()).toBe(401)
    const staleRefresh = await request.post(`${config.apiURL}/auth/refresh`, { headers: { Cookie: oldCookie } })
    expect(staleRefresh.status()).toBe(401)
    const replay = await request.post(`${config.apiURL}/auth/reset-password`, { data: { token, password: newPassword } })
    expect(replay.status()).toBe(400)
    expect((await replay.json() as { error: { code: string } }).error.code).toBe('INVALID_RESET_TOKEN')
    const oldLoginRetry = await request.post(`${config.apiURL}/auth/login`, { data: { email: user.email, password: oldPassword } })
    expect(oldLoginRetry.status()).toBe(401)

    const restored = await signIn(browser, { user, password: newPassword }, testInfo, errors)
    pages.push(restored)
    const staleAfterSignIn = await request.post(`${config.apiURL}/auth/refresh`, { headers: { Cookie: oldCookie } })
    expect(staleAfterSignIn.status()).toBe(401)
    await restored.reload()
    await expect(restored.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
    expect(errors).toEqual([])
    await testInfo.attach('password-recovery-record', {
      body: JSON.stringify({
        user_id: user.id,
        email_delivered: true,
        previous_browser_logged_out: true,
        old_access_status: staleAccess.status(),
        old_refresh_status: staleRefresh.status(),
        old_refresh_after_new_sign_in_status: staleAfterSignIn.status(),
        reused_link_status: replay.status(),
        new_password_sign_in_and_reload: true,
      }),
      contentType: 'application/json',
    })
  } finally {
    await Promise.all(pages.map((page) => page.context().close()))
    // Only the generated recovery account is changed; mailbox and existing history stay available.
    await json(await request.post(`${config.apiURL}/users/${user.id}/deactivate`, { headers }))
  }
})
