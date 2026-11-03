import {
  ArrowClockwise,
  ArrowLeft,
  ArrowRight,
  FunnelSimple,
  MagnifyingGlass,
  Ticket as TicketIcon,
} from '@phosphor-icons/react'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Link } from 'react-router-dom'

import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { useSessionStore } from '@/store/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Input } from '@/components/ui/Input'
import { Select } from '@/components/ui/Select'
import { ticketKeys, type TicketListFilters } from '@/features/tickets/keys'
import {
  TICKET_PRIORITY_VARIANTS,
  TICKET_STATUS_LABELS,
  TICKET_STATUS_VARIANTS,
  ticketPriorityLabel,
} from '@/features/tickets/presentation'
import type { components } from '@/types/api'
import { cn } from '@/utils/cn'

type Ticket = components['schemas']['TicketRead']
type TicketStatus = components['schemas']['TicketStatus']
type TicketPriority = components['schemas']['TicketPriority']
type TicketSortKey = components['schemas']['TicketSortKey']
type SortOrder = components['schemas']['SortOrder']

const PAGE_SIZE = 50

const STATUSES = Object.entries(TICKET_STATUS_LABELS).map(([value, label]) => ({
  value: value as TicketStatus,
  label,
}))

function localStartOfDay(value: string): string | undefined {
  if (!value) return undefined
  return new Date(`${value}T00:00:00`).toISOString()
}

function localDayAfter(value: string): string | undefined {
  if (!value) return undefined
  const nextDay = new Date(`${value}T00:00:00`)
  nextDay.setDate(nextDay.getDate() + 1)
  return nextDay.toISOString()
}

function formatUpdatedAt(value: string): string {
  const date = new Date(value)
  const today = new Date()
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate()
  const time = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(date)
  if (sameDay) return `Today, ${time}`
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date)
}

function formatRemaining(seconds: number): string {
  const totalMinutes = Math.floor(Math.abs(seconds) / 60)
  const days = Math.floor(totalMinutes / (60 * 24))
  const hours = Math.floor((totalMinutes % (60 * 24)) / 60)
  const minutes = totalMinutes % 60
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

function SlaPosition({ ticket }: { ticket: Ticket }) {
  const response = ticket.sla?.response
  if (!response) return <span className="text-ink-subtle">Not available</span>

  if (response.state === 'met') return <Badge variant="success" size="sm">Met</Badge>
  if (response.state === 'breached' || response.remaining_seconds <= 0) {
    return (
      <span className="text-xs font-medium text-danger-text">
        Overdue {formatRemaining(response.remaining_seconds)}
      </span>
    )
  }

  return (
    <span className={cn('text-xs font-medium', response.state === 'warning' ? 'text-warning-text' : 'text-ink-muted')}>
      {formatRemaining(response.remaining_seconds)} left
    </span>
  )
}

function TicketRow({ ticket }: { ticket: Ticket }) {
  return (
    <tr className="border-t border-border-subtle">
      <td className="py-3.5 pr-5 pl-5 align-middle">
        <div className="flex min-w-0 flex-col gap-1">
          <Link to={`/tickets/${ticket.id}`} className="text-xs font-medium tracking-wide text-ink-subtle hover:text-brand">#{ticket.number}</Link>
          <Link to={`/tickets/${ticket.id}`} className="line-clamp-1 max-w-[34rem] text-sm font-semibold text-ink hover:text-brand">{ticket.subject}</Link>
          <span className="line-clamp-1 max-w-[34rem] text-xs text-ink-muted">
            {ticket.category ? `${ticket.category}${ticket.subcategory ? ` · ${ticket.subcategory}` : ''}` : 'No category'}
          </span>
        </div>
      </td>
      <td className="py-3.5 pr-5 align-middle">
        <Badge variant={TICKET_STATUS_VARIANTS[ticket.status]} size="sm">{TICKET_STATUS_LABELS[ticket.status]}</Badge>
      </td>
      <td className="py-3.5 pr-5 align-middle">
        <Badge variant={TICKET_PRIORITY_VARIANTS[ticket.priority]} size="sm" dot>
          {ticketPriorityLabel(ticket.priority)}
        </Badge>
      </td>
      <td className="py-3.5 pr-5 align-middle"><SlaPosition ticket={ticket} /></td>
      <td className="py-3.5 pr-5 text-xs text-ink-muted align-middle">{formatUpdatedAt(ticket.updated_at)}</td>
    </tr>
  )
}

function TicketRowsSkeleton() {
  return (
    <div className="flex flex-col divide-y divide-border-subtle" aria-label="Loading tickets">
      {Array.from({ length: 6 }, (_, index) => (
        <div key={index} className="flex items-center gap-5 px-5 py-4">
          <div className="flex flex-1 flex-col gap-2">
            <div className="h-3 w-12 animate-pulse rounded bg-surface-sunken" />
            <div className="h-4 w-2/3 animate-pulse rounded bg-surface-sunken" />
            <div className="h-3 w-1/3 animate-pulse rounded bg-surface-sunken" />
          </div>
          <div className="h-5 w-20 animate-pulse rounded-full bg-surface-sunken" />
          <div className="hidden h-5 w-16 animate-pulse rounded-full bg-surface-sunken md:block" />
        </div>
      ))}
    </div>
  )
}

export function TicketQueuePage() {
  const user = useSessionStore((state) => state.user)
  const [searchParams, setSearchParams] = useSearchParams()
  const urlQuery = searchParams.get('q') ?? ''
  const [searchValue, setSearchValue] = useState({
    source: urlQuery,
    value: urlQuery,
    committed: urlQuery.trim(),
  })

  // A search submitted from the global header changes the URL while this page
  // is already mounted. Reset this component's draft during render so it never
  // paints the old query for a frame.
  if (searchValue.source !== urlQuery) {
    setSearchValue({ source: urlQuery, value: urlQuery, committed: urlQuery.trim() })
  }

  const [status, setStatus] = useState<TicketStatus | ''>('')
  const [priority, setPriority] = useState<TicketPriority | ''>('')
  const [assignment, setAssignment] = useState<'all' | 'mine' | 'unassigned'>('all')
  const [sortValue, setSortValue] = useState('updated_at:desc')
  const [createdAfter, setCreatedAfter] = useState('')
  const [createdBefore, setCreatedBefore] = useState('')
  const [offset, setOffset] = useState(0)

  useEffect(() => {
    const timer = setTimeout(() => {
      setSearchValue((current) =>
        current.source === urlQuery ? { ...current, committed: current.value.trim() } : current,
      )
    }, 250)
    return () => clearTimeout(timer)
  }, [searchValue.value, urlQuery])

  const filters = useMemo<TicketListFilters>(() => {
    const [sort, order] = sortValue.split(':') as [TicketSortKey, SortOrder]
    return {
      ...(status ? { status } : {}),
      ...(priority ? { priority } : {}),
      ...(assignment === 'mine' && user ? { assigned_agent_id: user.id } : {}),
      ...(assignment === 'unassigned' ? { unassigned: true } : {}),
      ...(searchValue.committed ? { q: searchValue.committed } : {}),
      ...(localStartOfDay(createdAfter) ? { created_after: localStartOfDay(createdAfter) } : {}),
      ...(localDayAfter(createdBefore) ? { created_before: localDayAfter(createdBefore) } : {}),
      sort,
      order,
      limit: PAGE_SIZE,
      offset,
    }
  }, [assignment, createdAfter, createdBefore, offset, priority, searchValue.committed, sortValue, status, user])

  const { data, error, isPending, isFetching, refetch } = useQuery({
    queryKey: ticketKeys.list(filters),
    queryFn: async () => {
      const response = await apiClient.get<Ticket[]>('/tickets', { params: filters })
      return response.data
    },
  })

  const tickets = data ?? []
  const hasFilters = Boolean(
    searchValue.committed || status || priority || assignment !== 'all' || createdAfter || createdBefore,
  )

  const resetFilters = () => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      next.delete('q')
      return next
    }, { replace: true })
    setSearchValue({ source: '', value: '', committed: '' })
    setStatus('')
    setPriority('')
    setAssignment('all')
    setSortValue('updated_at:desc')
    setCreatedAfter('')
    setCreatedBefore('')
    setOffset(0)
  }

  if (!user) return null

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="flex flex-col gap-6">
        <PageHeader
          title="Tickets"
          description="A live view of the work in your support queue. Changes from your team appear automatically."
          actions={
            data !== undefined ? (
              <Badge variant="neutral" size="md">
                {tickets.length} on this page
              </Badge>
            ) : null
          }
        />

        <Card variant="bezel" className="overflow-hidden">
          <div className="flex flex-col gap-4 border-b border-border-subtle bg-surface-sunken/40 p-4 sm:p-5">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
              <div className="min-w-0 flex-1">
                <Input
                  type="search"
                  value={searchValue.value}
                  onChange={(event) => {
                    setSearchValue((current) => ({ ...current, value: event.target.value }))
                    setOffset(0)
                  }}
                  placeholder="Search subject, message, customer, or ticket number"
                  aria-label="Search tickets"
                  maxLength={200}
                  leadingIcon={<MagnifyingGlass />}
                  className="h-10 bg-surface"
                />
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <label className="sr-only" htmlFor="ticket-priority-filter">Priority</label>
                <Select
                  id="ticket-priority-filter"
                  value={priority}
                  onChange={(event) => {
                    setPriority(event.target.value as TicketPriority | '')
                    setOffset(0)
                  }}
                  className="h-10 min-w-32 bg-surface"
                  aria-label="Filter by priority"
                >
                  <option value="">Any priority</option>
                  <option value="urgent">Urgent</option>
                  <option value="high">High</option>
                  <option value="medium">Medium</option>
                  <option value="low">Low</option>
                </Select>

                <label className="sr-only" htmlFor="ticket-assignment-filter">Assignment</label>
                <Select
                  id="ticket-assignment-filter"
                  value={assignment}
                  onChange={(event) => {
                    setAssignment(event.target.value as 'all' | 'mine' | 'unassigned')
                    setOffset(0)
                  }}
                  className="h-10 min-w-36 bg-surface"
                  aria-label="Filter by assignment"
                >
                  <option value="all">Any assignment</option>
                  <option value="mine">Assigned to me</option>
                  <option value="unassigned">Unassigned</option>
                </Select>

                <Button
                  variant="secondary"
                  size="md"
                  aria-label="Refresh ticket queue"
                  onClick={() => void refetch()}
                  disabled={isFetching}
                  leadingIcon={<ArrowClockwise className={cn('size-4', isFetching && 'animate-spin')} />}
                  className="h-10 px-3"
                >
                  <span className="sr-only sm:not-sr-only">Refresh</span>
                </Button>
              </div>
            </div>

            <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
              <div className="flex min-w-0 items-center gap-1 overflow-x-auto pb-1" role="group" aria-label="Filter by status">
                <button
                  type="button"
                  aria-pressed={!status}
                  onClick={() => { setStatus(''); setOffset(0) }}
                  className={cn(
                    'shrink-0 rounded-full px-3 py-1.5 text-xs font-medium transition-colors',
                    !status ? 'bg-ink text-canvas' : 'text-ink-muted hover:bg-surface-hover hover:text-ink',
                  )}
                >
                  All statuses
                </button>
                {STATUSES.map((item) => (
                  <button
                    key={item.value}
                    type="button"
                    aria-pressed={status === item.value}
                    onClick={() => { setStatus(item.value); setOffset(0) }}
                    className={cn(
                      'shrink-0 rounded-full px-3 py-1.5 text-xs font-medium transition-colors',
                      status === item.value ? 'bg-ink text-canvas' : 'text-ink-muted hover:bg-surface-hover hover:text-ink',
                    )}
                  >
                    {item.label}
                  </button>
                ))}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <span className="inline-flex items-center gap-1.5 pr-1 text-xs font-medium text-ink-muted">
                  <FunnelSimple className="size-3.5" />
                  Refine
                </span>
                <label className="sr-only" htmlFor="ticket-created-after">Created after</label>
                <Input
                  id="ticket-created-after"
                  type="date"
                  value={createdAfter}
                  onChange={(event) => { setCreatedAfter(event.target.value); setOffset(0) }}
                  aria-label="Created after"
                  className="h-9 w-36 bg-surface text-xs"
                />
                <span className="text-xs text-ink-subtle">to</span>
                <label className="sr-only" htmlFor="ticket-created-before">Created through</label>
                <Input
                  id="ticket-created-before"
                  type="date"
                  value={createdBefore}
                  onChange={(event) => { setCreatedBefore(event.target.value); setOffset(0) }}
                  aria-label="Created through"
                  className="h-9 w-36 bg-surface text-xs"
                />
                <label className="sr-only" htmlFor="ticket-sort">Sort tickets</label>
                <Select
                  id="ticket-sort"
                  value={sortValue}
                  onChange={(event) => { setSortValue(event.target.value); setOffset(0) }}
                  aria-label="Sort tickets"
                  className="h-9 min-w-40 bg-surface text-xs"
                >
                  <option value="updated_at:desc">Recently updated</option>
                  <option value="created_at:desc">Newest created</option>
                  <option value="created_at:asc">Oldest created</option>
                  <option value="priority:desc">Highest priority</option>
                  <option value="number:desc">Ticket number</option>
                </Select>
                {hasFilters && (
                  <button
                    type="button"
                    onClick={resetFilters}
                    className="rounded px-2 py-1 text-xs font-medium text-brand-text hover:underline"
                  >
                    Clear filters
                  </button>
                )}
              </div>
            </div>
          </div>

          {isPending ? (
            <TicketRowsSkeleton />
          ) : error ? (
            <ErrorState
              title="The queue couldn’t load"
              description="Your filters are still here. Try again in a moment."
              onRetry={() => void refetch()}
            />
          ) : tickets.length === 0 ? (
            <EmptyState
              icon={<TicketIcon />}
              title={hasFilters ? 'No tickets match these filters' : 'Your queue is clear'}
              description={hasFilters ? 'Adjust the search or filters to see more work.' : 'New customer requests will appear here as they arrive.'}
              {...(hasFilters
                ? { action: <Button variant="secondary" size="sm" onClick={resetFilters}>Clear filters</Button> }
                : {})}
            />
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[860px] border-collapse text-left">
                  <thead className="bg-surface-sunken/30">
                    <tr className="text-[0.6875rem] font-semibold tracking-[0.08em] text-ink-subtle uppercase">
                      <th scope="col" className="py-3 pr-5 pl-5">Ticket</th>
                      <th scope="col" className="py-3 pr-5">Status</th>
                      <th scope="col" className="py-3 pr-5">Priority</th>
                      <th scope="col" className="py-3 pr-5">Response SLA</th>
                      <th scope="col" className="py-3 pr-5">Last updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tickets.map((ticket) => <TicketRow key={ticket.id} ticket={ticket} />)}
                  </tbody>
                </table>
              </div>

              <div className="flex items-center justify-between gap-4 border-t border-border-subtle px-4 py-3 sm:px-5">
                <p className="text-xs text-ink-muted" aria-live="polite">
                  {isFetching ? 'Updating queue…' : `${tickets.length} ticket${tickets.length === 1 ? '' : 's'} on this page`}
                </p>
                <div className="flex items-center gap-2">
                  <span className="mr-1 text-xs tabular-nums text-ink-muted">Page {Math.floor(offset / PAGE_SIZE) + 1}</span>
                  <Button
                    variant="secondary"
                    size="sm"
                    aria-label="Previous page"
                    disabled={offset === 0 || isFetching}
                    onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}
                    leadingIcon={<ArrowLeft className="size-3.5" />}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    aria-label="Next page"
                    disabled={tickets.length < PAGE_SIZE || isFetching}
                    onClick={() => setOffset((current) => current + PAGE_SIZE)}
                    trailingIcon={<ArrowRight className="size-3.5" />}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          )}
        </Card>
      </div>
    </AppShell>
  )
}
