import { expect, test } from '@playwright/test'

import { getTenant, settings, signIn } from './helpers'

test('saved theme applies before application modules load under production CSP', async ({ browser }, testInfo) => {
  const context = await browser.newContext({
    baseURL: settings(testInfo).baseURL,
    ignoreHTTPSErrors: Boolean(testInfo.project.use.ignoreHTTPSErrors),
    colorScheme: 'light',
  })
  try {
    await context.addInitScript(() => localStorage.setItem('supportflow.theme', 'dark'))
    const page = await context.newPage()
    const cspErrors: string[] = []
    page.on('console', (message) => {
      if (message.type() === 'error' && /content security policy|violates.*directive/i.test(message.text())) cspErrors.push(message.text())
    })
    // React cannot repair a failed bootstrap while its application modules are blocked.
    await page.route('**/assets/*.js', (route) => route.abort())
    const bootstrap = page.waitForResponse((response) => new URL(response.url()).pathname === '/theme-init.js')
    await page.goto('/login')
    const response = await bootstrap
    expect(response.status()).toBe(200)
    expect(response.headers()['cache-control']).toBe('no-cache')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    expect(cspErrors).toEqual([])
  } finally {
    await context.close()
  }
})

test('HTTPS admin session rotates its secure cookie, loads lazy screens under CSP, and signs out', async ({ browser, request }, testInfo) => {
  const { admin } = await getTenant(request, testInfo)
  const errors: string[] = []
  const page = await signIn(browser, admin, testInfo, errors)
  const { baseURL } = settings(testInfo)
  try {
    const cookie = (await page.context().cookies()).find((item) => item.name === 'sf_refresh')
    // Assert flags without including the credential value in failure reports.
    expect(cookie && { secure: cookie.secure, httpOnly: cookie.httpOnly, sameSite: cookie.sameSite, path: cookie.path })
      .toEqual({ secure: true, httpOnly: true, sameSite: 'Lax', path: '/api/v1/auth' })

    const refresh = page.waitForResponse((response) => response.url() === `${baseURL}/api/v1/auth/refresh` && response.request().method() === 'POST')
    await page.reload()
    expect((await refresh).status()).toBe(200)
    await expect(page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
    const rotated = (await page.context().cookies()).find((item) => item.name === 'sf_refresh')
    expect(Boolean(rotated?.value && rotated.value !== cookie?.value)).toBe(true)

    const screen = page.waitForResponse((response) => /\/assets\/PreferencesPage-[^/]+\.js$/.test(new URL(response.url()).pathname))
    await page.getByRole('button', { name: 'Account menu', exact: true }).click()
    await page.getByRole('menuitem', { name: 'Your profile', exact: true }).click()
    expect((await screen).status()).toBe(200)
    await expect(page.getByRole('heading', { level: 1, name: 'Account preferences' })).toBeVisible()
    await expect(page.getByText(admin.user.email, { exact: true })).toBeVisible()

    const document = await page.goto('/preferences')
    expect(document?.status()).toBe(200)
    expect(document?.headers()['content-security-policy']).toContain("script-src 'self'")
    expect(document?.headers()['cache-control']).toBe('no-cache')
    await expect(page.getByRole('heading', { level: 1, name: 'Account preferences' })).toBeVisible()
    await expect(page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
    expect(errors).toEqual([])

    await page.getByRole('button', { name: 'Account menu', exact: true }).click()
    await page.getByRole('menuitem', { name: 'Sign out', exact: true }).click()
    await expect(page).toHaveURL(`${baseURL}/login`)
    expect((await page.context().cookies()).some((item) => item.name === 'sf_refresh')).toBe(false)
    await page.goto('/preferences')
    await expect(page).toHaveURL(`${baseURL}/login`)
    await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
    expect(errors).toEqual([])
  } finally {
    await page.context().close()
  }
})
