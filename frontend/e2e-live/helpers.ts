import { randomUUID } from 'node:crypto'
import { expect, type APIRequestContext, type Browser, type Page, type TestInfo } from '@playwright/test'

import type { components } from '../src/types/api'

type User = components['schemas']['UserRead']
export type Ticket = components['schemas']['TicketRead']
type Role = 'manager' | 'agent' | 'customer'
type Account = { user: User; password: string }

const password = 'LocalBrowserPassw0rd!23'
let tenant: Promise<Awaited<ReturnType<typeof createTenant>>> | undefined

export function settings(testInfo: TestInfo) {
  const production = testInfo.project.name === 'production'
  const baseURL = testInfo.project.use.baseURL ?? 'http://127.0.0.1:4174'
  const origin = production ? baseURL : 'http://127.0.0.1:8000'
  return {
    production,
    baseURL,
    apiURL: `${origin}/api/v1`,
    readinessURL: `${origin}/health/ready`,
    mailpitURL: `http://127.0.0.1:${production ? 8026 : 8025}`,
    domain: production ? 'supportflow-production-browser.example.com' : 'supportflow-live.example.com',
  }
}

export async function json<T>(response: Awaited<ReturnType<APIRequestContext['get']>>): Promise<T> {
  // Report setup status without printing tokens or credentials.
  expect(response.ok(), `Expected successful HTTP response, received ${response.status()}`).toBe(true)
  return response.json() as Promise<T>
}

async function createTenant(request: APIRequestContext, config: ReturnType<typeof settings>) {
  const { apiURL, domain, production } = config
  const email = `admin@${domain}`
  const workspace = production ? 'Local production browser checks' : 'Local live browser checks'
  let login = await request.post(`${apiURL}/auth/login`, { data: { email, password } })
  if (login.status() === 401) {
    await json<components['schemas']['RegistrationResponse']>(await request.post(`${apiURL}/auth/register`, {
      data: { organization_name: workspace, name: 'Live Browser Admin', email, password },
    }))
    const verified = await verifyTestAccount(request, config, email, workspace)
    login = await request.post(`${apiURL}/auth/login`, { data: { email, password, organization_slug: verified.organization_slug } })
  } else if (login.status() === 403 && (await login.json()).error?.code === 'EMAIL_NOT_VERIFIED') {
    const verified = await verifyTestAccount(request, config, email, workspace)
    login = await request.post(`${apiURL}/auth/login`, { data: { email, password, organization_slug: verified.organization_slug } })
  }
  const token = await json<components['schemas']['TokenResponse']>(login)
  const headers = { Authorization: `Bearer ${token.access_token}` }
  const admin = await json<User>(await request.get(`${apiURL}/auth/me`, { headers }))
  const customers = await json<components['schemas']['CustomerRead'][]>(await request.get(`${apiURL}/customers`, { headers }))
  const customer = customers.find((record) => record.email === `customer@${domain}`) ??
    await json<components['schemas']['CustomerRead']>(await request.post(`${apiURL}/customers`, {
      headers, data: { name: 'Live Browser Customer', email: `customer@${domain}` },
    }))
  const users = await json<User[]>(await request.get(`${apiURL}/users`, { headers }))
  const accounts = {} as Record<Role, Account>
  for (const role of ['manager', 'agent', 'customer'] as const) {
    const user = users.find((record) => record.email === `${role}@${domain}`) ?? await json<User>(await request.post(`${apiURL}/users`, {
      headers,
      data: {
        name: `Live Browser ${role}`, email: `${role}@${domain}`, password, role,
        customer_id: role === 'customer' ? customer.id : null,
      },
    }))
    if (user.email_verified_at === null) {
      await verifyTestAccount(request, config, user.email, workspace)
    }
    accounts[role] = { user, password }
  }
  return { accounts, admin: { user: admin, password }, headers, customer }
}

export async function verificationLink(
  request: APIRequestContext, config: ReturnType<typeof settings>, email: string, workspace: string,
): Promise<string> {
  type Mail = { ID: string; Subject: string; To: Array<{ Address: string }> }
  let link: string | undefined
  await expect.poll(async () => {
    const inbox = await json<{ messages: Mail[] }>(await request.get(`${config.mailpitURL}/api/v1/search`, { params: { query: `to:${email}` } }))
    for (const mail of inbox.messages) {
      if (!mail.Subject.includes('Verify your email') || !mail.Subject.endsWith(`— ${workspace}`) || !mail.To.some((recipient) => recipient.Address === email)) continue
      const body = await json<{ Text: string }>(await request.get(`${config.mailpitURL}/api/v1/message/${mail.ID}`))
      link = body.Text.match(/https?:\/\/[^\s]+\/verify-email#token=[^\s]+/)?.[0]
      if (link) return true
    }
    return false
  }, { timeout: 30_000, message: 'The email worker delivers the workspace verification link' }).toBe(true)
  if (!link) throw new Error('The verification email did not include a link')
  return link
}

export async function verifyTestAccount(
  request: APIRequestContext, config: ReturnType<typeof settings>, email: string, workspace: string,
): Promise<components['schemas']['VerifyEmailResponse']> {
  await json(await request.post(`${config.apiURL}/auth/resend-verification`, { data: { email } }))
  const link = await verificationLink(request, config, email, workspace)
  const token = new URLSearchParams(new URL(link).hash.slice(1)).get('token')
  if (!token) throw new Error('The verification link did not include a fragment credential')
  return json<components['schemas']['VerifyEmailResponse']>(await request.post(`${config.apiURL}/auth/verify-email`, { data: { token } }))
}

export async function getTenant(request: APIRequestContext, testInfo: TestInfo) {
  const config = settings(testInfo)
  const ready = await json<{ status: string }>(await request.get(config.readinessURL))
  expect(ready.status).toBe('ready')
  await json(await request.get(`${config.mailpitURL}/api/v1/info`))
  tenant ??= createTenant(request, config)
  return tenant
}

export async function createWorld(request: APIRequestContext, testInfo: TestInfo) {
  const config = settings(testInfo)
  const { accounts, admin, headers, customer } = await getTenant(request, testInfo)
  const suffix = randomUUID().slice(0, 8)
  const ticket = await json<Ticket>(await request.post(`${config.apiURL}/tickets`, {
    headers,
    data: { customer_id: customer.id, subject: `Live delivery check ${suffix}`, description: 'Verify the complete support workflow.' },
  }))
  return { accounts, admin, headers, ticket }
}

export async function signIn(browser: Browser, account: Account, testInfo: TestInfo, errors: string[]): Promise<Page> {
  const context = await browser.newContext({
    baseURL: settings(testInfo).baseURL,
    ignoreHTTPSErrors: Boolean(testInfo.project.use.ignoreHTTPSErrors),
  })
  const page = await context.newPage()
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error' && /content security policy|violates.*directive/i.test(message.text())) {
      errors.push(message.text())
    }
  })
  try {
    await page.goto('/login')
    await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
    await page.getByRole('textbox', { name: 'Email', exact: true }).fill(account.user.email)
    await page.getByLabel(/^Password\s*\*?$/).fill(account.password)
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    await expect(page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
    return page
  } catch (error) {
    await context.close()
    throw error
  }
}
