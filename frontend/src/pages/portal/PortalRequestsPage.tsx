import { ArrowRight, MagnifyingGlass, Plus, Ticket } from '@phosphor-icons/react'
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { ticketKeys } from '@/features/tickets/keys'
import { TICKET_STATUS_LABELS, TICKET_STATUS_VARIANTS } from '@/features/tickets/presentation'
import type { components } from '@/types/api'
import { useSessionStore } from '@/store/auth'

type TicketRead = components['schemas']['TicketRead']
const PAGE_SIZE = 50

function formatUpdatedAt(value: string): string {
  const date = new Date(value)
  const today = new Date()
  const sameDay = date.toDateString() === today.toDateString()
  return new Intl.DateTimeFormat(undefined, sameDay
    ? { hour: 'numeric', minute: '2-digit' }
    : { month: 'short', day: 'numeric' }).format(date)
}

function RequestRow({ ticket }: { ticket: TicketRead }) {
  return (
    <Link
      to={`/tickets/${ticket.id}`}
      className="group grid gap-3 px-4 py-4 transition-colors hover:bg-surface-sunken/60 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-focus sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-5"
    >
      <div className="flex min-w-0 items-start gap-3">
        <span className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-xl bg-brand-subtle text-brand-text">
          <Ticket aria-hidden="true" className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <span className="font-mono text-[0.6875rem] text-ink-subtle">#{ticket.number}</span>
            <Badge variant={TICKET_STATUS_VARIANTS[ticket.status]} size="sm" dot>
              {TICKET_STATUS_LABELS[ticket.status]}
            </Badge>
          </div>
          <p className="truncate text-sm font-semibold text-ink group-hover:text-brand-text">{ticket.subject}</p>
          <p className="mt-1 line-clamp-1 text-xs text-ink-muted">{ticket.description}</p>
        </div>
      </div>
      <div className="flex items-center justify-between gap-3 pl-12 sm:justify-end sm:pl-0">
        <span className="text-[0.6875rem] text-ink-subtle">Updated {formatUpdatedAt(ticket.updated_at)}</span>
        <ArrowRight aria-hidden="true" className="size-4 text-ink-subtle transition-transform group-hover:translate-x-0.5 group-hover:text-brand-text" />
      </div>
    </Link>
  )
}

export function PortalRequestsPage() {
  const user = useSessionStore((state) => state.user)
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [committedSearch, setCommittedSearch] = useState('')
  const [closedOnly, setClosedOnly] = useState(false)
  const [offset, setOffset] = useState(0)

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setCommittedSearch(search.trim())
      setOffset(0)
    }, 250)
    return () => window.clearTimeout(timeout)
  }, [search])

  const filters = {
    ...(committedSearch ? { q: committedSearch } : {}),
    ...(closedOnly ? { status: 'closed' as const } : {}),
    sort: 'updated_at' as const,
    order: 'desc' as const,
    limit: PAGE_SIZE,
    offset,
  }
  const ticketsQuery = useQuery({
    queryKey: ticketKeys.list(filters),
    queryFn: async () => (await apiClient.get<TicketRead[]>('/tickets', { params: filters })).data,
  })

  if (!user) return null
  const tickets = ticketsQuery.data ?? []
  const hasNextPage = tickets.length === PAGE_SIZE

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-5xl flex-col gap-6">
        <PageHeader
          title="Your requests"
          description="Track your support conversations and get back to the people helping you."
          actions={<Button leadingIcon={<Plus aria-hidden="true" />} onClick={() => navigate('/portal/new')}>New request</Button>}
        />

        <Card variant="bezel" className="overflow-hidden">
          <div className="flex flex-col gap-3 border-b border-border-subtle bg-surface-sunken/35 p-4 sm:flex-row sm:items-center sm:p-5">
            <div className="min-w-0 flex-1">
              <Input
                type="search"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search your requests"
                aria-label="Search your requests"
                maxLength={200}
                leadingIcon={<MagnifyingGlass aria-hidden="true" />}
                className="bg-surface"
              />
            </div>
            <div className="flex rounded-xl border border-border-subtle bg-surface p-1" aria-label="Request status filter">
              <button
                type="button"
                aria-pressed={!closedOnly}
                onClick={() => { setClosedOnly(false); setOffset(0) }}
                className={`rounded-lg px-3 py-2 text-xs font-medium transition-colors ${!closedOnly ? 'bg-ink text-canvas' : 'text-ink-muted hover:text-ink'}`}
              >All requests</button>
              <button
                type="button"
                aria-pressed={closedOnly}
                onClick={() => { setClosedOnly(true); setOffset(0) }}
                className={`rounded-lg px-3 py-2 text-xs font-medium transition-colors ${closedOnly ? 'bg-ink text-canvas' : 'text-ink-muted hover:text-ink'}`}
              >Closed</button>
            </div>
          </div>

          {ticketsQuery.isPending ? (
            <div className="divide-y divide-border-subtle" aria-label="Loading your requests">
              {Array.from({ length: 5 }, (_, index) => (
                <div key={index} className="flex items-center gap-3 px-5 py-5">
                  <Skeleton className="size-9 rounded-xl" />
                  <div className="flex flex-1 flex-col gap-2">
                    <Skeleton className="h-3 w-2/5" />
                    <Skeleton className="h-3 w-3/5" />
                  </div>
                </div>
              ))}
            </div>
          ) : ticketsQuery.error ? (
            <ErrorState title="Your requests couldn’t load" error={ticketsQuery.error} onRetry={() => { void ticketsQuery.refetch() }} />
          ) : tickets.length ? (
            <div className="divide-y divide-border-subtle">
              {tickets.map((ticket) => <RequestRow key={ticket.id} ticket={ticket} />)}
            </div>
          ) : (
            <EmptyState
              icon={<Ticket aria-hidden="true" />}
              title={committedSearch ? 'No matching requests' : closedOnly ? 'No closed requests' : 'Your support history starts here'}
              description={committedSearch
                ? 'Try a different ticket number or search phrase.'
                : closedOnly ? 'Requests you close will be saved here.' : 'Send our team a request and follow every reply in one place.'}
              action={!committedSearch && !closedOnly ? <Button onClick={() => navigate('/portal/new')}>Create your first request</Button> : undefined}
            />
          )}

          {!ticketsQuery.isPending && !ticketsQuery.error && tickets.length > 0 && (
            <div className="flex items-center justify-between border-t border-border-subtle px-4 py-3 sm:px-5">
              <p className="text-xs text-ink-subtle">Showing {offset + 1}–{offset + tickets.length}</p>
              <div className="flex gap-2">
                <Button variant="secondary" size="sm" disabled={offset === 0} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>Previous</Button>
                <Button variant="secondary" size="sm" disabled={!hasNextPage} onClick={() => setOffset((value) => value + PAGE_SIZE)}>Next</Button>
              </div>
            </div>
          )}
        </Card>
      </div>
    </AppShell>
  )
}
