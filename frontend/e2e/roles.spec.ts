import { accounts, customerId, expect, test, ticketId } from './fixtures'

test.describe('role-based browser workflows', () => {
  test('admin can sign in, read audit activity, and create a team account', async ({ page, api }) => {
    await api.signIn('admin')

    const navigation = page.getByRole('navigation', { name: 'Main' })
    await navigation.getByRole('link', { name: 'Audit log' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Audit log' })).toBeVisible()
    await navigation.getByRole('link', { name: 'Team' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Team' })).toBeVisible()
    await page.getByRole('button', { name: 'Add team member' }).click()

    const form = page.getByRole('dialog', { name: 'Create an account' })
    await form.getByRole('textbox', { name: 'Full name' }).fill('Rowan Patel')
    await form.getByRole('textbox', { name: 'Email address' }).fill('rowan@example.test')
    await form.getByLabel('Initial password').fill('new-member-password')
    await form.getByRole('button', { name: 'Create account' }).click()

    const confirmation = page.getByRole('dialog', { name: 'Account is ready' })
    await expect(confirmation.getByRole('textbox', { name: 'Email address' })).toHaveValue('rowan@example.test')
    await confirmation.getByRole('button', { name: 'Done' }).click()
    await expect(page.getByText('Rowan Patel', { exact: true })).toBeVisible()
    expect(api.requests).toContainEqual({
      role: 'admin', method: 'POST', path: '/users',
      body: { name: 'Rowan Patel', email: 'rowan@example.test', password: 'new-member-password', role: 'agent', customer_id: null },
    })
  })

  test('manager can assign and reprioritize tickets without admin-only team controls', async ({ page, api }) => {
    await api.signIn('manager')

    const navigation = page.getByRole('navigation', { name: 'Main' })
    await expect(navigation.getByRole('link', { name: 'Audit log' })).toHaveCount(0)
    await navigation.getByRole('link', { name: 'Team' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Team' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add team member' })).toHaveCount(0)
    await expect(page.getByRole('combobox', { name: 'Role for Priya Raman' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Deactivate', exact: true })).toHaveCount(0)

    await page.goto(`/tickets/${ticketId}`)
    await page.getByRole('combobox', { name: 'Assign ticket to an agent' }).selectOption(accounts.agent.id)
    await page.getByRole('button', { name: 'Save assignment' }).click()
    await expect(page.getByRole('button', { name: 'Start working' })).toBeVisible()
    await page.getByRole('combobox', { name: 'Change ticket priority' }).selectOption('high')
    await expect(page.getByRole('combobox', { name: 'Change ticket priority' })).toHaveValue('high')
    await expect(page.getByText('High priority', { exact: true }).and(page.locator('span'))).toBeVisible()
    expect(api.requests).toContainEqual({
      role: 'manager', method: 'POST', path: `/tickets/${ticketId}/assign`,
      body: { assigned_agent_id: accounts.agent.id },
    })
    expect(api.requests).toContainEqual({
      role: 'manager', method: 'POST', path: `/tickets/${ticketId}/priority`, body: { priority: 'high' },
    })
  })

  test('agent sees assigned-work navigation and an explanation on restricted routes', async ({ page, api }) => {
    await api.signIn('agent')

    const navigation = page.getByRole('navigation', { name: 'Main' })
    await expect(navigation.getByRole('link', { name: 'Tickets' })).toBeVisible()
    await expect(navigation.getByRole('link', { name: 'Team' })).toHaveCount(0)
    await expect(navigation.getByRole('link', { name: 'Audit log' })).toHaveCount(0)

    for (const path of ['/audit', '/users']) {
      await page.goto(path)
      await expect(page).toHaveURL(new RegExp(`${path}$`))
      await expect(page.getByRole('alert')).toContainText('You do not have access to this page')
    }
    expect(api.requests.filter((request) => request.path === '/audit-logs' || request.path === '/users')).toHaveLength(0)
  })

  test('customer can sign in and submit a request without staff navigation', async ({ page, api }) => {
    await api.signIn('customer')

    const navigation = page.getByRole('navigation', { name: 'Main' })
    await expect(navigation.getByRole('link', { name: 'New request' })).toBeVisible()
    await expect(navigation.getByRole('link', { name: 'Tickets' })).toHaveCount(0)
    await expect(navigation.getByRole('link', { name: 'Team' })).toHaveCount(0)

    await navigation.getByRole('link', { name: 'New request' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Tell us what you need' })).toBeVisible()
    await page.getByRole('textbox', { name: 'What can we help with?' }).fill('Browser test: delivery update')
    await page.getByRole('textbox', { name: 'Describe what happened' }).fill('Please share the latest delivery estimate.')
    await page.getByRole('button', { name: 'Submit request' }).click()

    await expect(page.getByRole('heading', { level: 1, name: 'Browser test: delivery update' })).toBeVisible()
    expect(api.requests).toContainEqual({
      role: 'customer', method: 'POST', path: '/tickets',
      body: { subject: 'Browser test: delivery update', description: 'Please share the latest delivery estimate.' },
    })
    expect(api.ticket.customer_id).toBe(customerId)
  })
})

test.describe('ticket conversations and lifecycle', () => {
  test('agent can work an assigned ticket and returns to the queue after reopening it', async ({ page, api }) => {
    api.ticket = { ...api.ticket, status: 'assigned', assigned_agent_id: accounts.agent.id }
    await api.openAs('agent')
    await expect(page.getByRole('combobox', { name: 'Assign ticket to an agent' })).toHaveCount(0)
    await expect(page.getByRole('combobox', { name: 'Change ticket priority' })).toHaveCount(0)

    await page.getByRole('button', { name: 'Start working' }).click()
    await expect(page.getByRole('button', { name: 'Resolve ticket' })).toBeVisible()
    await page.getByRole('textbox', { name: 'Your reply' }).fill('Your package will arrive tomorrow.')
    await page.getByRole('button', { name: 'Send reply' }).click()
    await expect(page.getByText('Your package will arrive tomorrow.', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: 'Internal note', exact: true }).click()
    await expect(page.getByText('Only your support team can see this note.', { exact: false })).toBeVisible()
    await page.getByRole('textbox', { name: 'Internal note' }).fill('Escalation reference: staff only.')
    await page.getByRole('button', { name: 'Add internal note' }).click()
    await expect(page.getByText('Escalation reference: staff only.', { exact: true })).toBeVisible()
    expect(api.requests).toContainEqual({
      role: 'agent', method: 'POST', path: `/tickets/${ticketId}/messages`, body: { body: 'Your package will arrive tomorrow.' },
    })
    expect(api.requests).toContainEqual({
      role: 'agent', method: 'POST', path: `/tickets/${ticketId}/notes`, body: { body: 'Escalation reference: staff only.' },
    })

    await page.getByRole('button', { name: 'Resolve ticket' }).click()
    await page.getByRole('button', { name: 'Close ticket', exact: true }).click()
    await page.getByRole('dialog', { name: 'Close ticket #99?' }).getByRole('button', { name: 'Close ticket' }).click()
    await expect(page.getByRole('textbox', { name: 'Your reply' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Send reply' })).toBeDisabled()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await page.getByRole('button', { name: 'Reopen ticket', exact: true }).click()
    await page.getByRole('dialog', { name: 'Reopen ticket #99?' }).getByRole('button', { name: 'Reopen ticket' }).click()
    await expect(page).toHaveURL(/\/tickets$/)
    await expect(page.getByRole('heading', { level: 1, name: 'Tickets' })).toBeVisible()
    await expect(page.getByRole('link', { name: /Delivery update/ })).toHaveCount(0)
    await expect(page.getByRole('textbox', { name: 'Your reply' })).toHaveCount(0)
    expect(api.ticket.assigned_agent_id).toBeNull()
    await page.reload()
    await expect(page.getByRole('heading', { level: 1, name: 'Tickets' })).toBeVisible()
    await expect(page.getByRole('link', { name: /Delivery update/ })).toHaveCount(0)
    expect(api.requests.filter((request) => request.path === `/tickets/${ticketId}/status`).map((request) => request.body))
      .toEqual([{ status: 'in_progress' }, { status: 'resolved' }])
  })

  test('customer can close and reopen their own request and reply again', async ({ page, api }) => {
    api.ticket = { ...api.ticket, status: 'resolved', assigned_agent_id: accounts.agent.id }
    await api.openAs('customer')

    await page.getByRole('button', { name: 'Close ticket', exact: true }).click()
    await page.getByRole('dialog', { name: 'Close ticket #99?' }).getByRole('button', { name: 'Close ticket' }).click()
    await expect(page.getByRole('textbox', { name: 'Your reply' })).toBeDisabled()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await page.getByRole('button', { name: 'Reopen ticket', exact: true }).click()
    await page.getByRole('dialog', { name: 'Reopen ticket #99?' }).getByRole('button', { name: 'Reopen ticket' }).click()

    await expect(page.getByText('Open', { exact: true })).toBeVisible()
    await expect(page.getByRole('textbox', { name: 'Your reply' })).toBeEnabled()
    await page.reload()
    await expect(page.getByRole('heading', { level: 1, name: 'Delivery update' })).toBeVisible()
    await page.getByRole('textbox', { name: 'Your reply' }).fill('I still need help with this delivery.')
    await page.getByRole('button', { name: 'Send reply' }).click()
    await expect(page.getByText('I still need help with this delivery.', { exact: true })).toBeVisible()
    expect(api.ticket.assigned_agent_id).toBeNull()
  })

  test('customer reads public replies and has no internal-note, assignment, or AI controls', async ({ page, api }) => {
    api.addMessage('The courier has confirmed your delivery date.')
    api.addMessage('Private escalation details.', 'agent', true)
    const draft = api.addMessage('Unapproved AI response.', 'agent', true)
    draft.sender_type = 'ai_draft'
    await api.openAs('customer', '/portal')
    await page.getByRole('link', { name: /Delivery update/ }).click()

    await expect(page.getByText('The courier has confirmed your delivery date.', { exact: true })).toBeVisible()
    await expect(page.getByText('1 messages', { exact: true })).toBeVisible()
    await expect(page.getByText('Private escalation details.', { exact: true })).toHaveCount(0)
    await expect(page.getByText('Unapproved AI response.', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Internal note', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Analyze ticket' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Draft a reply' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Review and send' })).toHaveCount(0)
    await expect(page.getByRole('combobox', { name: 'Assign ticket to an agent' })).toHaveCount(0)
    await expect(page.getByRole('combobox', { name: 'Change ticket priority' })).toHaveCount(0)
    expect(api.requests.filter((request) => request.path.includes('/ai/') || request.path.startsWith('/customers'))).toHaveLength(0)
  })

  test('a failed reply stays in the composer and can be retried without duplication', async ({ page, api }) => {
    api.ticket = { ...api.ticket, status: 'assigned', assigned_agent_id: accounts.agent.id }
    await api.openAs('agent')
    api.failNextReply = true
    const composer = page.getByRole('textbox', { name: 'Your reply' })
    await composer.fill('Keep this reply until it is delivered.')
    await page.getByRole('button', { name: 'Send reply' }).click()
    await expect(page.getByText('Message could not be sent', { exact: true })).toBeVisible()
    await expect(composer).toHaveValue('Keep this reply until it is delivered.')
    expect(api.messages).toHaveLength(0)

    await page.getByRole('button', { name: 'Send reply' }).click()
    await expect(page.getByText('Keep this reply until it is delivered.', { exact: true })).toBeVisible()
    await expect(composer).toHaveValue('')
    expect(api.messages).toHaveLength(1)
    expect(api.requests.filter((request) => request.method === 'POST' && request.path === `/tickets/${ticketId}/messages`)).toHaveLength(2)
  })

  test('an AI draft is reviewed and edited before the acceptance request sends a public reply', async ({ page, api }) => {
    api.ticket = { ...api.ticket, status: 'assigned', assigned_agent_id: accounts.agent.id }
    const draft = api.addMessage('Unreviewed delivery estimate.', 'agent', true)
    draft.sender_type = 'ai_draft'
    await api.openAs('agent')
    await page.getByRole('button', { name: 'Review and send' }).click()
    await page.getByRole('textbox', { name: 'Edit AI generated reply' }).fill('The confirmed delivery date is Friday.')
    await page.getByRole('button', { name: 'Send edited reply' }).click()

    await expect(page.getByText('The confirmed delivery date is Friday.', { exact: true })).toBeVisible()
    expect(api.requests).toContainEqual({
      role: 'agent', method: 'POST', path: `/tickets/${ticketId}/ai/drafts/${draft.id}/accept`,
      body: { body: 'The confirmed delivery date is Friday.' },
    })
    expect(api.messages.at(-1)?.is_internal).toBe(false)
  })
})

test.describe('session and realtime integration', () => {
  test('developer diagnostics routes are absent from the production app', async ({ page, api }) => {
    await api.install('admin')
    for (const path of ['/dev/kit', '/dev/health']) {
      await page.goto(path)
      await expect(page.getByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible()
    }
  })

  test('a restored session can open its profile and remains signed out after a reload', async ({ page, api }) => {
    api.ticket = { ...api.ticket, status: 'assigned', assigned_agent_id: accounts.agent.id }
    await api.openAs('agent')
    await expect(page).toHaveURL(new RegExp(`/tickets/${ticketId}$`))
    await expect(page.getByRole('heading', { level: 1, name: 'Delivery update' })).toBeVisible()
    await page.getByRole('button', { name: 'Account menu' }).click()
    await page.getByRole('menuitem', { name: 'Your profile' }).click()
    await expect(page).toHaveURL(/\/preferences$/)
    await expect(page.getByRole('heading', { level: 1, name: 'Account preferences' })).toBeVisible()
    await expect(page.getByText(accounts.agent.email, { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Account menu' }).click()
    await page.getByRole('menuitem', { name: 'Sign out' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
    await page.reload()
    await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
    expect(api.requests.filter((request) => request.path === '/auth/logout')).toHaveLength(1)
    expect(api.requests.filter((request) => request.path === '/auth/refresh')).toHaveLength(2)
  })

  test('a realtime reply refreshes the customer conversation and ticket status without navigation', async ({ page, api }) => {
    api.addMessage('We are checking with the courier.')
    await api.openAs('customer')
    await expect(page.getByText('1 messages', { exact: true })).toBeVisible()
    const reads = api.requests.filter((request) => request.method === 'GET' && request.path === `/tickets/${ticketId}/messages`).length

    api.addMessage('The courier expects delivery tomorrow.')
    api.ticket = { ...api.ticket, status: 'waiting_for_customer' }
    api.emitTicket('customer', 'ticket.message_added')

    await expect(page.getByText('The courier expects delivery tomorrow.', { exact: true })).toBeVisible()
    await expect(page.getByText('2 messages', { exact: true })).toBeVisible()
    await expect(page.getByText('Waiting for customer', { exact: true })).toBeVisible()
    expect(api.requests.filter((request) => request.method === 'GET' && request.path === `/tickets/${ticketId}/messages`).length).toBeGreaterThan(reads)
    await expect(page).toHaveURL(new RegExp(`/tickets/${ticketId}$`))
  })

  test('reconnecting refreshes changes missed while the socket was disconnected', async ({ page, api }) => {
    api.ticket = { ...api.ticket, status: 'assigned', assigned_agent_id: accounts.agent.id }
    await api.openAs('agent')
    await expect(page.getByText('0 messages', { exact: true })).toBeVisible()
    const authentications = api.socketAuthentications.agent
    api.ticket = { ...api.ticket, status: 'in_progress' }
    api.addMessage('This reply arrived during the service restart.')
    await api.disconnect('agent')

    await expect.poll(() => api.socketAuthentications.agent).toBeGreaterThan(authentications)
    await expect(page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
    await expect(page.getByText('This reply arrived during the service restart.', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Resolve ticket' })).toBeVisible()
  })
})
