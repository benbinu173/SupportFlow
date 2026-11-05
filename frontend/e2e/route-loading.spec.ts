import { accounts, expect, test, ticketId } from './fixtures'

const ticketChunk = '**/assets/TicketDetailPage-*.js'

test('anonymous visitors reach sign-in without downloading the protected ticket screen', async ({ page, api }) => {
  const scripts: string[] = []
  page.on('request', (request) => {
    if (request.resourceType() === 'script') scripts.push(request.url())
  })
  await api.install('agent', false)
  await page.goto(`/tickets/${ticketId}`)
  await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
  expect(scripts.some((url) => /\/TicketDetailPage-[^/]+\.js$/.test(url))).toBe(false)
  expect(scripts.some((url) => /\/AnalyticsPage-[^/]+\.js$/.test(url))).toBe(false)
})

test('a delayed screen download shows a loading state and then renders the ticket', async ({ page, api }) => {
  let release!: () => void
  const held = new Promise<void>((resolve) => { release = resolve })
  let requested = false
  await page.route(ticketChunk, async (route) => {
    requested = true
    await held
    await route.continue()
  })
  api.ticket = { ...api.ticket, status: 'assigned', assigned_agent_id: accounts.agent.id }
  await api.install('agent')
  try {
    await page.goto(`/tickets/${ticketId}`, { waitUntil: 'domcontentloaded' })
    await expect.poll(() => requested).toBe(true)
    await expect(page.getByRole('status', { name: 'Loading page' })).toBeVisible()
    release()
    await expect(page.getByRole('heading', { level: 1, name: 'Delivery update' })).toBeVisible()
    await expect(page.getByRole('status', { name: 'Loading page' })).toHaveCount(0)
  } finally {
    release()
  }
})

test('a failed screen download reaches the error page and reload recovers', async ({ page, api }) => {
  let fail = true
  await page.route(ticketChunk, (route) => fail ? route.abort('failed') : route.continue())
  api.ticket = { ...api.ticket, status: 'assigned', assigned_agent_id: accounts.agent.id }
  await api.install('agent')
  await page.goto(`/tickets/${ticketId}`)
  await expect(page.getByRole('heading', { level: 1, name: 'Something went wrong' })).toBeVisible()
  // Import failures are caught by RouteError; they must not escape as page errors.
  fail = false
  await page.getByRole('button', { name: 'Reload', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Delivery update' })).toBeVisible()
})
