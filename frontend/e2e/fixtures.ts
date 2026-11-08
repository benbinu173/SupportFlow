import { expect, test as base, type Page, type WebSocketRoute } from '@playwright/test'

import type { RealtimeEnvelope, TicketEventEnvelope } from '../src/services/realtime/protocol'
import type { components } from '../src/types/api'

type User = components['schemas']['UserRead']
type Ticket = components['schemas']['TicketRead']
type Message = components['schemas']['MessageRead']
type TicketEvent = components['schemas']['TicketEventRead']
type KnowledgeDocument = components['schemas']['KnowledgeDocumentRead']
export type Role = User['role']

export const now = '2026-11-04T00:00:00Z'
export const customerId = '00000000-0000-4000-8000-000000000004'
export const ticketId = '00000000-0000-4000-8000-000000000099'
export const password = 'browser-test-password'

function account(role: Role, id: string, name: string): User {
  return {
    id,
    name,
    email: `${role}@example.test`,
    role,
    is_active: true,
    customer_id: role === 'customer' ? customerId : null,
    created_at: now,
    last_login_at: null,
  }
}

export const accounts: Record<Role, User> = {
  admin: account('admin', '00000000-0000-4000-8000-000000000001', 'Dana Whitfield'),
  manager: account('manager', '00000000-0000-4000-8000-000000000002', 'Marco Ferreira'),
  agent: account('agent', '00000000-0000-4000-8000-000000000003', 'Priya Raman'),
  customer: account('customer', customerId, 'Helena Marsh'),
}

const range = { start: now, end: '2026-11-05T00:00:00Z', bucket: 'day' as const }
const emptyOverview: components['schemas']['AnalyticsOverview'] = {
  range,
  totals: {
    total: 0, open: 0, resolved: 0, closed: 0,
    by_status: { open: 0, assigned: 0, in_progress: 0, waiting_for_customer: 0, resolved: 0, closed: 0 },
  },
  volume: [],
  response_time: { average_seconds: null, count: 0 },
  resolution_time: { average_seconds: null, count: 0 },
  ai_usage: {
    calls: 0, failed_calls: 0, cached_calls: 0, prompt_tokens: 0,
    completion_tokens: 0, cost_usd: '0.000000', by_operation: [],
  },
}
const emptySla: components['schemas']['AnalyticsSLA'] = {
  range,
  compliance: {
    response: { timer: 'response', met: 0, breached: 0, rate: null },
    resolution: { timer: 'resolution', met: 0, breached: 0, rate: null },
    met: 0, breached: 0, rate: null,
  },
  overdue: 0,
  open_tickets: 0,
  risks: [],
}
const emptyTickets: components['schemas']['AnalyticsTickets'] = { range, by_priority: [], by_category: [] }
const emptyAgents: components['schemas']['AnalyticsAgents'] = { range, agents: [], total: 0 }
const emptySentiment: components['schemas']['AnalyticsSentiment'] = { range, buckets: [], analysed: 0, unanalysed: 0 }

interface ApiRequest {
  role: Role
  method: string
  path: string
  body: unknown
}

/** Per-test API state. Unhandled requests fail the test instead of returning invented success. */
class WorkflowApi {
  ticket: Ticket = {
    id: ticketId, number: 99, customer_id: customerId, assigned_agent_id: null,
    subject: 'Delivery update', description: 'Please share the latest delivery estimate.',
    status: 'open', priority: 'medium', category: null, subcategory: null,
    sentiment: null, sentiment_confidence: null, ai_recommended_priority: null,
    ai_classification_confidence: null, first_response_at: null, resolved_at: null,
    closed_at: null, created_at: now, updated_at: now, sla: null,
  }
  messages: Message[] = []
  events: TicketEvent[] = []
  users: User[] = Object.values(accounts).map((user) => ({ ...user }))
  overview = structuredClone(emptyOverview)
  knowledgeDocuments: KnowledgeDocument[] = []
  requests: ApiRequest[] = []
  unexpectedRequests: string[] = []
  pageErrors: string[] = []
  socketAuthentications: Record<Role, number> = { admin: 0, manager: 0, agent: 0, customer: 0 }
  failNextReply = false
  failNextKnowledgeAnswer = false
  readonly page: Page

  private sequence = 100
  private sockets = new Map<Role, Set<WebSocketRoute>>()

  constructor(page: Page) {
    this.page = page
    page.on('pageerror', (error) => this.pageErrors.push(error.message))
  }

  private nextId(): string {
    return `00000000-0000-4000-8000-${String(this.sequence++).padStart(12, '0')}`
  }

  private canReadTicket(role: Role): boolean {
    if (role === 'agent') return this.ticket.assigned_agent_id === accounts.agent.id
    if (role === 'customer') return this.ticket.customer_id === accounts.customer.customer_id
    return true
  }

  addMessage(body: string, sender: Role = 'agent', internal = false): Message {
    const message: Message = {
      id: this.nextId(), ticket_id: ticketId, body, is_internal: internal,
      sender_type: sender === 'customer' ? 'customer' : 'agent',
      sender_user_id: accounts[sender].id, created_at: now,
    }
    this.messages.push(message)
    return message
  }

  private addEvent(role: Role, event_type: TicketEvent['event_type'], from: string | null, to: string | null) {
    this.events.push({
      id: this.nextId(), event_type, actor_user_id: accounts[role].id,
      from_value: from, to_value: to, extra_data: {}, created_at: now,
    })
  }

  private recordKnowledgeUsage(operation: 'embed' | 'knowledge_answer', successful: boolean): void {
    const usage = this.overview.ai_usage
    const cost = successful ? 0.0032 : 0
    usage.calls += 1
    usage.failed_calls += successful ? 0 : 1
    usage.prompt_tokens += successful ? 1200 : 0
    usage.completion_tokens += successful && operation === 'knowledge_answer' ? 80 : 0
    usage.cost_usd = (Number(usage.cost_usd) + cost).toFixed(6)
    const entry = usage.by_operation.find((row) => row.operation === operation)
    if (entry) {
      entry.calls += 1
      entry.cost_usd = (Number(entry.cost_usd) + cost).toFixed(6)
    } else {
      usage.by_operation.push({ operation, calls: 1, cost_usd: cost.toFixed(6) })
    }
  }

  finishIngestion(documentId: string, outcome: 'completed' | 'failed'): void {
    const document = this.knowledgeDocuments.find((item) => item.id === documentId)
    expect(document).toBeDefined()
    if (!document) throw new Error('The source must be created before it is processed.')
    document.status = outcome
    document.is_published = outcome === 'completed'
    document.chunk_count = outcome === 'completed' ? 1 : 0
    document.processed_at = outcome === 'completed' ? now : null
    document.error_message = outcome === 'failed' ? 'The AI service is temporarily unavailable.' : null
    this.recordKnowledgeUsage('embed', outcome === 'completed')
  }

  async install(role: Role, restored = true): Promise<void> {
    let authenticated = restored
    const token = `e2e-${role}-access-token`
    const tokenResponse = { access_token: token, token_type: 'bearer', expires_in: 900 }
    const customer: components['schemas']['CustomerRead'] = {
      id: customerId, name: accounts.customer.name, email: accounts.customer.email,
      phone: null, external_reference: null, extra_data: {}, created_at: now, updated_at: now,
    }

    await this.page.routeWebSocket('**/ws', (socket) => {
      const active = this.sockets.get(role) ?? new Set<WebSocketRoute>()
      this.sockets.set(role, active)
      socket.onMessage((data) => {
        expect(JSON.parse(data.toString())).toEqual({ type: 'auth', token })
        active.add(socket)
        this.socketAuthentications[role] += 1
        socket.send(JSON.stringify({ type: 'authenticated' }))
      })
      socket.onClose(() => active.delete(socket))
    })

    await this.page.route('**/api/v1/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      const path = url.pathname.slice('/api/v1'.length)
      const method = request.method()
      const body = request.postData() ? request.postDataJSON() : undefined
      this.requests.push({ role, method, path, body })

      const reply = (json: unknown, status = 200) => route.fulfill({ status, json })
      const refuse = (message: string, status = 401) =>
        reply({ error: { code: status === 401 ? 'AUTHENTICATION_REQUIRED' : 'FORBIDDEN', message } }, status)

      if (path === '/auth/refresh' && method === 'POST') {
        return authenticated ? reply(tokenResponse) : refuse('Sign in to continue.')
      }
      if (path === '/auth/login' && method === 'POST') {
        if (body.email !== accounts[role].email || body.password !== password) return refuse('Email or password is incorrect.')
        authenticated = true
        return reply(tokenResponse)
      }
      if (!authenticated || request.headers().authorization !== `Bearer ${token}`) return refuse('Sign in to continue.')
      if (path === '/auth/me' && method === 'GET') return reply(accounts[role])
      if (path === '/auth/logout' && method === 'POST') {
        authenticated = false
        return route.fulfill({ status: 204 })
      }
      if (path === '/notifications/unread-count' && method === 'GET') return reply({ unread: 0 })
      if (path === '/analytics/overview' && method === 'GET') return reply(this.overview)
      if (path === '/analytics/sla' && method === 'GET') return reply(emptySla)
      if (path === '/analytics/tickets' && method === 'GET') return reply(emptyTickets)
      if (path === '/analytics/agents' && method === 'GET' && (role === 'admin' || role === 'manager')) return reply(emptyAgents)
      if (path === '/analytics/sentiment' && method === 'GET') return reply(emptySentiment)
      if (path === '/knowledge' && method === 'GET' && role !== 'customer') {
        const query = url.searchParams.get('q')?.toLowerCase()
        return reply(this.knowledgeDocuments.filter((document) => !query || document.title.toLowerCase().includes(query)))
      }
      if (path === '/knowledge' && method === 'POST' && role === 'admin') {
        const payload = body as components['schemas']['KnowledgeDocumentCreate']
        const document: KnowledgeDocument = {
          id: this.nextId(), title: payload.title, source_type: payload.url ? 'url' : 'manual',
          status: 'pending', is_published: false, chunk_count: 0, error_message: null,
          created_by_id: accounts[role].id, created_at: now, updated_at: now, processed_at: null,
        }
        this.knowledgeDocuments.unshift(document)
        return reply(document, 201)
      }
      if (path === '/knowledge/search' && method === 'POST' && role !== 'customer') {
        const successful = !this.failNextKnowledgeAnswer
        this.failNextKnowledgeAnswer = false
        this.recordKnowledgeUsage('knowledge_answer', successful)
        if (!successful) return reply({ error: { code: 'AI_SERVICE_ERROR', message: 'The AI service is temporarily unavailable.' } }, 503)
        const document = this.knowledgeDocuments.find((item) => item.is_published)
        const answer: components['schemas']['KnowledgeAnswerRead'] = {
          answer: 'Refunds take five working days.',
          sources: document ? [{
            document_id: document.id, document_title: document.title, chunk_index: 0,
            excerpt: 'Refunds take five working days.', similarity: 0.9,
          }] : [],
        }
        return reply(answer)
      }
      if (path === '/notifications' && method === 'GET') return reply([])
      if (path === '/audit-logs' && method === 'GET' && role === 'admin') return reply([])
      if (path === '/users' && method === 'GET' && (role === 'admin' || role === 'manager')) return reply(this.users)
      if (path === '/users' && method === 'POST' && role === 'admin') {
        const payload = body as components['schemas']['UserCreate']
        const user: User = {
          id: this.nextId(), name: payload.name, email: payload.email, role: payload.role,
          customer_id: payload.customer_id ?? null, is_active: true, created_at: now, last_login_at: null,
        }
        this.users.push(user)
        return reply(user, 201)
      }
      if (path === '/customers' && method === 'GET' && role !== 'customer') return reply([customer])
      if (path === `/customers/${customerId}` && method === 'GET' && role !== 'customer') return reply(customer)
      if (path === '/tickets' && method === 'GET') {
        const status = url.searchParams.get('status')
        const query = url.searchParams.get('q')?.toLowerCase()
        const visible = this.canReadTicket(role)
        return reply(visible && (!status || this.ticket.status === status) &&
          (!query || this.ticket.subject.toLowerCase().includes(query)) ? [this.ticket] : [])
      }
      if (path === '/tickets' && method === 'POST') {
        const payload = body as components['schemas']['TicketCreate']
        this.ticket = { ...this.ticket, subject: payload.subject, description: payload.description }
        return reply(this.ticket, 201)
      }
      if ((path === `/tickets/${ticketId}` || path.startsWith(`/tickets/${ticketId}/`)) && !this.canReadTicket(role)) {
        return reply({ error: { code: 'NOT_FOUND', message: 'Ticket not found.' } }, 404)
      }
      if (path === `/tickets/${ticketId}` && method === 'GET') return reply(this.ticket)
      if (path === `/tickets/${ticketId}/events` && method === 'GET') {
        return reply(role === 'customer' ? this.events.filter((event) => event.event_type !== 'internal_note_added') : this.events)
      }
      if (path === `/tickets/${ticketId}/messages` && method === 'GET') {
        return reply(role === 'customer'
          ? this.messages.filter((message) => !message.is_internal && message.sender_type !== 'ai_draft')
          : this.messages)
      }
      if (path === `/tickets/${ticketId}/attachments` && method === 'GET') return reply([])
      if (path === `/tickets/${ticketId}/ai/analyses` && method === 'GET' && role !== 'customer') return reply([])
      if (path === `/tickets/${ticketId}/assign` && method === 'POST' && (role === 'admin' || role === 'manager')) {
        const assigned = (body as components['schemas']['TicketAssign']).assigned_agent_id ?? null
        this.addEvent(role, assigned ? 'assigned' : 'unassigned', this.ticket.assigned_agent_id, assigned)
        this.ticket = { ...this.ticket, assigned_agent_id: assigned, status: assigned ? 'assigned' : 'open' }
        return reply(this.ticket)
      }
      if (path === `/tickets/${ticketId}/priority` && method === 'POST' && (role === 'admin' || role === 'manager')) {
        const { priority } = body as { priority: Ticket['priority'] }
        this.addEvent(role, 'priority_changed', this.ticket.priority, priority)
        this.ticket = { ...this.ticket, priority }
        return reply(this.ticket)
      }
      if (path === `/tickets/${ticketId}/status` && method === 'POST' && role !== 'customer') {
        const { status } = body as { status: Ticket['status'] }
        this.addEvent(role, 'status_changed', this.ticket.status, status)
        this.ticket = { ...this.ticket, status, resolved_at: status === 'resolved' ? now : null }
        return reply(this.ticket)
      }
      if (path === `/tickets/${ticketId}/close` && method === 'POST') {
        this.ticket = { ...this.ticket, status: 'closed', closed_at: now }
        return reply(this.ticket)
      }
      if (path === `/tickets/${ticketId}/reopen` && method === 'POST') {
        this.ticket = { ...this.ticket, status: 'open', assigned_agent_id: null, resolved_at: null, closed_at: null }
        this.addEvent(role, 'reopened', 'closed', 'open')
        return reply(this.ticket)
      }
      if (path === `/tickets/${ticketId}/messages` && method === 'POST') {
        if (this.failNextReply) {
          this.failNextReply = false
          return reply({ error: { code: 'SERVICE_UNAVAILABLE', message: 'Please try sending your reply again.' } }, 503)
        }
        const message = this.addMessage((body as { body: string }).body, role)
        this.addEvent(role, 'message_added', null, null)
        return reply(message, 201)
      }
      if (path === `/tickets/${ticketId}/notes` && method === 'POST' && role !== 'customer') {
        const message = this.addMessage((body as { body: string }).body, role, true)
        this.addEvent(role, 'internal_note_added', null, null)
        return reply(message, 201)
      }
      const draft = this.messages.find((message) => path === `/tickets/${ticketId}/ai/drafts/${message.id}/accept`)
      if (draft && method === 'POST' && role !== 'customer') {
        return reply(this.addMessage((body as { body: string }).body, role), 201)
      }

      this.unexpectedRequests.push(`${role}: ${method} ${path}`)
      return reply({ error: { code: 'UNHANDLED_TEST_REQUEST', message: `No fixture for ${method} ${path}` } }, 500)
    })
  }

  async openAs(role: Role, path = `/tickets/${ticketId}`): Promise<void> {
    await this.install(role)
    await this.page.goto(path)
    await expect(this.page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
  }

  async signIn(role: Role): Promise<void> {
    await this.install(role, false)
    await this.page.goto('/login')
    // The form appears after anonymous bootstrap settles; no fixed delay or store injection.
    await expect(this.page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
    await this.page.getByRole('textbox', { name: 'Email', exact: true }).fill(accounts[role].email)
    // Label lookup includes the decorative required marker, unlike role names.
    await this.page.getByLabel(/^Password\s*\*?$/).fill(password)
    await this.page.getByRole('button', { name: 'Sign in', exact: true }).click()
    await expect(this.page.getByRole('heading', {
      level: 1, name: role === 'customer' ? 'Your requests' : `Welcome back, ${accounts[role].name.split(' ')[0]}`,
    })).toBeVisible()
    await expect(this.page.getByRole('status').filter({ hasText: /^Live$/ })).toBeVisible()
  }

  emitTicket(role: Role, type: TicketEventEnvelope['type']): void {
    const envelope: RealtimeEnvelope = {
      type, organization_id: '00000000-0000-4000-8000-000000000050',
      ticket_id: ticketId, ticket_number: this.ticket.number,
      assigned_agent_id: this.ticket.assigned_agent_id, customer_id: customerId,
      from_value: null, to_value: null, internal: type === 'ticket.note_added',
    }
    const active = this.sockets.get(role)
    expect(active?.size).toBeGreaterThan(0)
    active?.forEach((socket) => socket.send(JSON.stringify(envelope)))
  }

  async disconnect(role: Role): Promise<void> {
    const active = this.sockets.get(role)
    expect(active?.size).toBeGreaterThan(0)
    const closing = [...active ?? []]
    closing.forEach((socket) => active?.delete(socket))
    await Promise.all(closing.map((socket) => socket.close({ code: 1012, reason: 'Service restart' })))
  }
}

export const test = base.extend<{ api: WorkflowApi }>({
  api: async ({ page }, provideApi) => {
    const api = new WorkflowApi(page)
    await provideApi(api)
    expect(api.unexpectedRequests, 'Every API call must have an explicit fixture').toEqual([])
    expect(api.pageErrors, 'The browser should render without uncaught errors').toEqual([])
  },
})

export { expect }
