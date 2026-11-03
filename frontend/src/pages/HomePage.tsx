import { ArrowRight, Clock, Lightning, Ticket } from '@phosphor-icons/react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Skeleton } from '@/components/ui/Skeleton'
import { analyticsKeys } from '@/features/analytics/keys'
import { can, PERMISSIONS } from '@/features/auth/permissions'
import { ticketKeys } from '@/features/tickets/keys'
import { TICKET_STATUS_LABELS, TICKET_STATUS_VARIANTS, ticketPriorityLabel } from '@/features/tickets/presentation'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { toApiError } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import type { components } from '@/types/api'

type AnalyticsOverview = components['schemas']['AnalyticsOverview']
type AnalyticsSLA = components['schemas']['AnalyticsSLA']
type TicketRead = components['schemas']['TicketRead']

function formatRemaining(seconds: number): string {
  const value = Math.abs(Math.floor(seconds))
  if (value >= 86_400) return `${Math.floor(value / 86_400)}d ${Math.floor((value % 86_400) / 3_600)}h`
  if (value >= 3_600) return `${Math.floor(value / 3_600)}h ${Math.floor((value % 3_600) / 60)}m`
  return `${Math.max(1, Math.floor(value / 60))}m`
}

function Metric({ label, value, note, variant = 'neutral' }: { label: string; value: string | number; note: string; variant?: 'neutral' | 'warning' | 'danger' }) {
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-col gap-3 p-4 sm:p-5">
        <p className="text-xs font-medium text-ink-muted">{label}</p>
        <p className={`text-3xl font-semibold tracking-[-0.04em] tabular-nums ${variant === 'danger' ? 'text-danger-text' : variant === 'warning' ? 'text-warning-text' : 'text-ink'}`}>{value}</p>
        <p className="text-[0.6875rem] text-ink-subtle">{note}</p>
      </div>
    </Card>
  )
}

function DashboardMetrics({ overview, sla }: { overview: AnalyticsOverview; sla?: AnalyticsSLA }) {
  const statuses = overview.totals.by_status
  const active = (statuses.open ?? 0) + (statuses.assigned ?? 0) + (statuses.in_progress ?? 0) + (statuses.waiting_for_customer ?? 0)
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Metric label="Active tickets" value={active} note={`Among ${overview.totals.total} tickets created in 30 days`} />
      <Metric label="Waiting for customer" value={statuses.waiting_for_customer ?? 0} note="Paused until the customer replies" />
      <Metric label="Resolved" value={overview.totals.resolved} note="Among tickets created in 30 days" />
      <Metric
        label="SLA overdue"
        value={sla?.overdue ?? '—'}
        note={sla ? `Across ${sla.open_tickets} active tickets` : 'SLA data unavailable'}
        variant={sla?.overdue ? 'danger' : 'neutral'}
      />
    </div>
  )
}

export function HomePage() {
  const user = useSessionStore((state) => state.user)
  const navigate = useNavigate()
  const role = user?.role
  const canViewSla = can(role, PERMISSIONS.SLA_VIEW)

  const overviewQuery = useQuery({
    queryKey: analyticsKeys.overview(),
    queryFn: async () => (await apiClient.get<AnalyticsOverview>('/analytics/overview')).data,
    enabled: Boolean(user && can(role, PERMISSIONS.ANALYTICS_OWN)),
  })
  const slaQuery = useQuery({
    queryKey: analyticsKeys.sla(),
    queryFn: async () => (await apiClient.get<AnalyticsSLA>('/analytics/sla', { params: { limit: 5 } })).data,
    enabled: Boolean(user && canViewSla && can(role, PERMISSIONS.ANALYTICS_OWN)),
    refetchInterval: 30_000,
  })
  const ticketsQuery = useQuery({
    queryKey: ticketKeys.list({ sort: 'updated_at', order: 'desc', limit: 6, offset: 0 }),
    queryFn: async () => (await apiClient.get<TicketRead[]>('/tickets', {
      params: { sort: 'updated_at', order: 'desc', limit: 6, offset: 0 },
    })).data,
    enabled: Boolean(user && can(role, PERMISSIONS.TICKET_LIST)),
  })

  if (!user) return null
  const greetingName = user.name.trim().split(/\s+/)[0] || user.name

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <PageHeader
          title={`Welcome back, ${greetingName}`}
          description={`Your ${user.role === 'agent' ? 'assigned' : 'team'} workload at a glance.`}
          actions={<Button onClick={() => navigate('/tickets')} leadingIcon={<Ticket aria-hidden="true" />}>Open ticket queue</Button>}
        />

        {overviewQuery.isPending ? (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Loading dashboard metrics">
            {Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-32 rounded-xl" />)}
          </div>
        ) : overviewQuery.error ? (
          <ErrorState title="Dashboard metrics couldn’t load" error={overviewQuery.error} onRetry={() => { void overviewQuery.refetch() }} />
        ) : overviewQuery.data ? (
          <DashboardMetrics overview={overviewQuery.data} sla={slaQuery.data} />
        ) : null}

        <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(18rem,0.8fr)]">
          <Card variant="bezel" className="overflow-hidden">
            <div className="flex items-center justify-between gap-4 border-b border-border-subtle px-5 py-4">
              <div className="flex items-center gap-2.5">
                <span className="grid size-8 place-items-center rounded-xl bg-brand-subtle text-brand-text"><Ticket aria-hidden="true" className="size-4" /></span>
                <div><h2 className="text-sm font-semibold text-ink">Recently updated</h2><p className="mt-0.5 text-xs text-ink-muted">The latest tickets you can access.</p></div>
              </div>
              <Link to="/tickets" className="inline-flex items-center gap-1 text-xs font-medium text-brand-text hover:underline">Queue <ArrowRight aria-hidden="true" className="size-3.5" /></Link>
            </div>

            {ticketsQuery.isPending ? (
              <div className="divide-y divide-border-subtle" aria-label="Loading recent tickets">
                {Array.from({ length: 5 }, (_, index) => <div key={index} className="flex items-center gap-3 px-5 py-4"><Skeleton className="size-8 rounded-full" /><div className="flex flex-1 flex-col gap-2"><Skeleton className="h-3 w-2/5" /><Skeleton className="h-3 w-3/5" /></div></div>)}
              </div>
            ) : ticketsQuery.error ? (
              <ErrorState title="Recent tickets couldn’t load" description={toApiError(ticketsQuery.error).message} onRetry={() => { void ticketsQuery.refetch() }} />
            ) : ticketsQuery.data?.length ? (
              <div className="divide-y divide-border-subtle">
                {ticketsQuery.data.map((ticket) => (
                  <Link key={ticket.id} to={`/tickets/${ticket.id}`} className="group flex items-center gap-3 px-4 py-4 transition-colors hover:bg-surface-sunken/50 sm:px-5">
                    <span className="grid size-8 shrink-0 place-items-center rounded-xl bg-brand-subtle text-brand-text"><Ticket aria-hidden="true" className="size-4" /></span>
                    <div className="min-w-0 flex-1"><p className="truncate text-sm font-medium text-ink group-hover:text-brand-text">{ticket.subject}</p><p className="mt-1 text-[0.6875rem] text-ink-subtle">#{ticket.number} · {ticketPriorityLabel(ticket.priority)}</p></div>
                    <Badge variant={TICKET_STATUS_VARIANTS[ticket.status]} size="sm">{TICKET_STATUS_LABELS[ticket.status]}</Badge>
                    <ArrowRight aria-hidden="true" className="hidden size-4 text-ink-subtle group-hover:text-brand-text sm:block" />
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState icon={<Ticket aria-hidden="true" />} title="No tickets in your queue" description="Tickets assigned to you or your organization will appear here." />
            )}
          </Card>

          <Card variant="bezel" className="overflow-hidden">
            <div className="flex items-center gap-2.5 border-b border-border-subtle px-5 py-4">
              <span className="grid size-8 place-items-center rounded-xl bg-warning-subtle text-warning-text"><Lightning aria-hidden="true" className="size-4" /></span>
              <div><h2 className="text-sm font-semibold text-ink">SLA attention</h2><p className="mt-0.5 text-xs text-ink-muted">Tickets nearest to a deadline.</p></div>
            </div>
            {slaQuery.isPending ? (
              <div className="flex flex-col gap-4 p-5" aria-label="Loading SLA risks">{Array.from({ length: 3 }, (_, index) => <div key={index} className="flex flex-col gap-2"><Skeleton className="h-3 w-3/5" /><Skeleton className="h-3 w-4/5" /></div>)}</div>
            ) : slaQuery.error ? (
              <ErrorState title="SLA risks couldn’t load" error={slaQuery.error} onRetry={() => { void slaQuery.refetch() }} />
            ) : slaQuery.data?.risks.length ? (
              <div className="divide-y divide-border-subtle">
                {slaQuery.data.risks.map((risk) => (
                  <Link key={risk.id} to={`/tickets/${risk.id}`} className="flex flex-col gap-2 px-5 py-4 transition-colors hover:bg-surface-sunken/50">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0"><p className="text-[0.6875rem] text-ink-subtle">Ticket #{risk.number}</p><p className="mt-1 line-clamp-2 text-sm font-medium text-ink">{risk.subject}</p></div>
                      <Badge variant={risk.state === 'breached' ? 'danger' : risk.state === 'warning' ? 'warning' : 'neutral'} size="sm">{risk.state === 'breached' ? 'Overdue' : formatRemaining(risk.remaining_seconds)}</Badge>
                    </div>
                    <p className="text-[0.6875rem] text-ink-subtle">{risk.timer === 'response' ? 'First response' : 'Resolution'} target</p>
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState icon={<Clock aria-hidden="true" />} title="No tickets at risk" description="Nothing is currently approaching or past an SLA deadline." className="py-12" />
            )}
          </Card>
        </div>
      </div>
    </AppShell>
  )
}
