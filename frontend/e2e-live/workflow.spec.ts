import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

import { createWorld, json, settings, signIn } from './helpers'

import type { components } from '../src/types/api'

type Ticket = components['schemas']['TicketRead']
type Message = components['schemas']['MessageRead']
type Notification = components['schemas']['NotificationRead']
type Mail = { ID: string; Subject: string; To: Array<{ Address: string }> }

async function confirm(page: Page, action: 'Close' | 'Reopen', number: number) {
  await page.getByRole('button', { name: `${action} ticket`, exact: true }).click()
  await page.getByRole('dialog', { name: `${action} ticket #${number}?` })
    .getByRole('button', { name: `${action} ticket`, exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
}

async function deliveredMail(request: APIRequestContext, address: string, title: string, ticket: Ticket, mailpitURL: string) {
  let delivered: Mail | undefined
  await expect.poll(async () => {
    const inbox = await json<{ messages: Mail[] }>(await request.get(`${mailpitURL}/api/v1/search`, {
      params: { query: `to:${address}` },
    }))
    delivered = undefined
    for (const mail of inbox.messages) {
      if (mail.Subject !== `[SupportFlow] ${title}` || !mail.To.some((recipient) => recipient.Address === address)) continue
      const body = await json<{ Text: string }>(await request.get(`${mailpitURL}/api/v1/message/${mail.ID}`))
      if (body.Text.includes(`#${ticket.number} - ${ticket.subject}`)) {
        delivered = mail
        break
      }
    }
    return Boolean(delivered)
  }, { timeout: 30_000, message: `Worker delivers ${title} to Mailpit` }).toBe(true)
  const body = await json<{ Text: string }>(await request.get(`${mailpitURL}/api/v1/message/${delivered!.ID}`))
  expect(body.Text).toContain(`#${ticket.number} - ${ticket.subject}`)
}

test('live manager, agent, and customer workflow persists and delivers realtime updates and email', async ({ browser, request }, testInfo) => {
  const { apiURL, mailpitURL } = settings(testInfo)
  const world = await createWorld(request, testInfo)
  const pages: Page[] = []
  const errors: string[] = []
  try {
    for (const role of ['manager', 'agent', 'customer'] as const) {
      const page = await signIn(browser, world.accounts[role], testInfo, errors)
      pages.push(page)
    }
    const [manager, agent, customer] = pages as [Page, Page, Page]
    const path = `/tickets/${world.ticket.id}`

    await agent.goto('/notifications')
    await expect(agent.getByRole('heading', { level: 1, name: 'Notifications' })).toBeVisible()
    await customer.goto(path)
    await expect(customer.getByRole('heading', { level: 1, name: world.ticket.subject })).toBeVisible()
    await manager.goto(path)
    await manager.getByRole('combobox', { name: 'Assign ticket to an agent' }).selectOption(world.accounts.agent.user.id)
    await manager.getByRole('button', { name: 'Save assignment' }).click()

    // Neither recipient reloads: notification and status arrive via real WebSockets.
    const assignment = agent.getByRole('article').filter({ hasText: world.ticket.subject })
    await expect(assignment.getByText('Ticket assigned to you', { exact: true })).toBeVisible()
    await expect(customer.getByText('Assigned', { exact: true })).toBeVisible()
    await deliveredMail(request, world.accounts.agent.user.email, 'Ticket assigned to you', world.ticket, mailpitURL)
    await assignment.getByRole('link', { name: 'View ticket', exact: true }).click()
    await agent.getByRole('button', { name: 'Start working' }).click()
    await expect(customer.getByText('In progress', { exact: true })).toBeVisible()

    const reply = 'Your delivery is confirmed for Friday.'
    await agent.getByRole('textbox', { name: 'Your reply' }).fill(reply)
    await agent.getByRole('button', { name: 'Send reply' }).click()
    await expect(customer.getByText(reply, { exact: true })).toBeVisible()
    await agent.getByRole('button', { name: 'Internal note', exact: true }).click()
    await agent.getByRole('textbox', { name: 'Internal note' }).fill('Private courier escalation reference.')
    await agent.getByRole('button', { name: 'Add internal note' }).click()
    await expect(agent.getByText('Private courier escalation reference.', { exact: true })).toBeVisible()
    await expect(customer.getByText('Private courier escalation reference.', { exact: true })).toHaveCount(0)

    await customer.getByRole('textbox', { name: 'Your reply' }).fill('Thank you for confirming the date.')
    await customer.getByRole('button', { name: 'Send reply' }).click()
    await expect(agent.getByText('Thank you for confirming the date.', { exact: true })).toBeVisible()
    await deliveredMail(request, world.accounts.agent.user.email, 'New reply from the customer', world.ticket, mailpitURL)
    await agent.getByRole('button', { name: 'Resolve ticket' }).click()
    await expect(customer.getByText('Resolved', { exact: true })).toBeVisible()
    await deliveredMail(request, world.accounts.customer.user.email, 'Your ticket has been resolved', world.ticket, mailpitURL)

    await customer.reload()
    await expect(customer.getByText(reply, { exact: true })).toBeVisible()
    await expect(customer.getByText('Resolved', { exact: true })).toBeVisible()
    await expect(customer.getByText('Private courier escalation reference.', { exact: true })).toHaveCount(0)
    await confirm(customer, 'Close', world.ticket.number)
    await expect(customer.getByRole('textbox', { name: 'Your reply' })).toBeDisabled()
    await expect(manager.getByText('Closed', { exact: true })).toBeVisible()
    await confirm(customer, 'Reopen', world.ticket.number)
    await expect(customer.getByRole('textbox', { name: 'Your reply' })).toBeEnabled()
    await expect(manager.getByText('Open', { exact: true })).toBeVisible()
    await expect(manager.getByRole('combobox', { name: 'Assign ticket to an agent' })).toHaveValue('')
    await customer.reload()
    await expect(customer.getByText('Open', { exact: true })).toBeVisible()

    // Read back over real HTTP independently of each page's query cache.
    const stored = await json<Ticket>(await request.get(`${apiURL}${path}`, { headers: world.headers }))
    expect(stored).toMatchObject({ status: 'open', assigned_agent_id: null, resolved_at: null, closed_at: null })
    expect(stored.first_response_at).not.toBeNull()
    const messages = await json<Message[]>(await request.get(`${apiURL}${path}/messages`, { headers: world.headers }))
    expect(messages.filter((message) => !message.is_internal).map((message) => message.body))
      .toEqual([reply, 'Thank you for confirming the date.'])
    expect(messages.filter((message) => message.is_internal).map((message) => message.body))
      .toEqual(['Private courier escalation reference.'])
    expect(errors).toEqual([])
    await testInfo.attach('live-workflow-record', {
      body: JSON.stringify({ ticket_id: stored.id, ticket_number: stored.number, subject: stored.subject, final_status: stored.status }),
      contentType: 'application/json',
    })
  } finally {
    await Promise.all(pages.map((page) => page.context().close()))
  }
})

test('an agent reopening a live ticket returns to the queue and loses detail access', async ({ browser, request }, testInfo) => {
  const { apiURL } = settings(testInfo)
  const world = await createWorld(request, testInfo)
  const path = `/tickets/${world.ticket.id}`
  await json(await request.post(`${apiURL}${path}/assign`, {
    headers: world.headers, data: { assigned_agent_id: world.accounts.agent.user.id },
  }))
  for (const status of ['in_progress', 'resolved'] as const) {
    await json(await request.post(`${apiURL}${path}/status`, { headers: world.headers, data: { status } }))
  }
  await json(await request.post(`${apiURL}${path}/close`, { headers: world.headers }))
  const errors: string[] = []
  const page = await signIn(browser, world.accounts.agent, testInfo, errors)
  try {
    await page.goto(path)
    await expect(page.getByRole('heading', { level: 1, name: world.ticket.subject })).toBeVisible()
    await confirm(page, 'Reopen', world.ticket.number)
    await expect(page).toHaveURL(/\/tickets$/)
    await expect(page.getByRole('link', { name: new RegExp(world.ticket.subject) })).toHaveCount(0)
    await page.reload()
    await expect(page.getByRole('heading', { level: 1, name: 'Tickets' })).toBeVisible()
    await expect(page.getByRole('link', { name: new RegExp(world.ticket.subject) })).toHaveCount(0)
    await page.goto(path)
    await expect(page.getByText('Ticket not found', { exact: true })).toBeVisible()
    const stored = await json<Ticket>(await request.get(`${apiURL}${path}`, { headers: world.headers }))
    expect(stored.status).toBe('open')
    expect(stored.assigned_agent_id).toBeNull()
    const notifications = await json<Notification[]>(await request.get(`${apiURL}/notifications`, { headers: world.headers }))
    expect(notifications.every((notification) => notification.ticket_id !== stored.id)).toBe(true)
    expect(errors).toEqual([])
  } finally {
    await page.context().close()
  }
})
