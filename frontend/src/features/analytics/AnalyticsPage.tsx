import { ArrowDown, ArrowUp, ChartLineUp, Clock, CurrencyDollar, Ticket, Warning } from '@phosphor-icons/react'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Skeleton } from '@/components/ui/Skeleton'
import { analyticsKeys } from '@/features/analytics/keys'
import { can, PERMISSIONS } from '@/features/auth/permissions'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { useSessionStore } from '@/store/auth'
import type { components } from '@/types/api'

type AnalyticsOverview = components['schemas']['AnalyticsOverview']
type AnalyticsTickets = components['schemas']['AnalyticsTickets']
type AnalyticsAgents = components['schemas']['AnalyticsAgents']
type AnalyticsSLA = components['schemas']['AnalyticsSLA']
type AnalyticsSentiment = components['schemas']['AnalyticsSentiment']
type WindowChoice = 7 | 30 | 90

const WINDOWS: WindowChoice[] = [7, 30, 90]
const PRIORITY_COLORS: Record<string, string> = {
  urgent: 'var(--color-danger)',
  high: 'var(--color-warning)',
  medium: 'var(--color-brand)',
  low: 'var(--color-ink-subtle)',
}

function windowBounds(days: WindowChoice) {
  const end = new Date()
  const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000)
  return { start: start.toISOString(), end: end.toISOString() }
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return '—'
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} sec`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} hr`
  return `${(seconds / 86400).toFixed(1)} days`
}

function formatCost(cost: string): string {
  const value = Number(cost)
  return Number.isFinite(value)
    ? new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(value)
    : '$0.00'
}

function PanelHeading({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="border-b border-border-subtle px-5 py-4">
      <h2 className="text-sm font-semibold text-ink">{title}</h2>
      <p className="mt-1 text-xs text-ink-muted">{detail}</p>
    </div>
  )
}

function Metric({ label, value, detail, icon, tone = 'brand' }: {
  label: string
  value: string
  detail: string
  icon: ReactNode
  tone?: 'brand' | 'warning' | 'danger' | 'success'
}) {
  const toneClasses = {
    brand: 'bg-brand-subtle text-brand-text',
    warning: 'bg-warning-subtle text-warning-text',
    danger: 'bg-danger-subtle text-danger-text',
    success: 'bg-success-subtle text-success-text',
  }
  return (
    <Card className="overflow-hidden">
      <div className="flex items-start justify-between gap-3 p-4 sm:p-5">
        <div className="min-w-0">
          <p className="text-xs font-medium text-ink-muted">{label}</p>
          <p className="mt-3 text-2xl font-semibold tracking-[-0.04em] tabular-nums text-ink">{value}</p>
          <p className="mt-1.5 text-[0.6875rem] text-ink-subtle">{detail}</p>
        </div>
        <span className={`grid size-9 shrink-0 place-items-center rounded-xl ${toneClasses[tone]}`}>{icon}</span>
      </div>
    </Card>
  )
}

function LoadingPanels() {
  return (
    <div className="space-y-5" aria-label="Loading analytics">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-28 rounded-xl" />)}</div>
      <Skeleton className="h-80 rounded-xl" />
      <div className="grid gap-5 lg:grid-cols-2"><Skeleton className="h-72 rounded-xl" /><Skeleton className="h-72 rounded-xl" /></div>
    </div>
  )
}

export function AnalyticsPage() {
  const user = useSessionStore((state) => state.user)
  const [days, setDays] = useState<WindowChoice>(30)
  const isOrgView = can(user?.role, PERMISSIONS.ANALYTICS_ORG)
  const bounds = useMemo(() => windowBounds(days), [days])
  const params = { start: bounds.start, end: bounds.end }
  const rangeKey = `${days}:${bounds.start.slice(0, 10)}`
  const enabled = Boolean(user && can(user.role, PERMISSIONS.ANALYTICS_OWN))

  const overview = useQuery({
    queryKey: analyticsKeys.overview(rangeKey),
    queryFn: async () => (await apiClient.get<AnalyticsOverview>('/analytics/overview', { params })).data,
    enabled,
  })
  const tickets = useQuery({
    queryKey: analyticsKeys.tickets(rangeKey),
    queryFn: async () => (await apiClient.get<AnalyticsTickets>('/analytics/tickets', { params })).data,
    enabled,
  })
  const sla = useQuery({
    queryKey: analyticsKeys.sla(rangeKey),
    queryFn: async () => (await apiClient.get<AnalyticsSLA>('/analytics/sla', { params: { ...params, limit: 6 } })).data,
    enabled: enabled && can(user?.role, PERMISSIONS.SLA_VIEW),
    refetchInterval: 30_000,
  })
  const sentiment = useQuery({
    queryKey: analyticsKeys.sentiment(rangeKey),
    queryFn: async () => (await apiClient.get<AnalyticsSentiment>('/analytics/sentiment', { params })).data,
    enabled,
  })
  const agents = useQuery({
    queryKey: analyticsKeys.agents(rangeKey),
    queryFn: async () => (await apiClient.get<AnalyticsAgents>('/analytics/agents', { params: { ...params, limit: 8 } })).data,
    enabled: enabled && isOrgView,
  })

  const chartData = useMemo(() => {
    const byDay = new Map((overview.data?.volume ?? []).map((point) => [point.bucket.slice(0, 10), point.count]))
    const end = new Date()
    return Array.from({ length: days }, (_, index) => {
      const day = new Date(end.getTime() - (days - index - 1) * 86_400_000)
      const key = day.toISOString().slice(0, 10)
      return { date: key, count: byDay.get(key) ?? 0, label: new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(day) }
    })
  }, [days, overview.data?.volume])

  if (!user) return null
  const firstError = overview.error ?? tickets.error ?? sentiment.error ?? sla.error ?? agents.error
  const allLoading = overview.isPending || tickets.isPending || sentiment.isPending || (can(user.role, PERMISSIONS.SLA_VIEW) && sla.isPending) || (isOrgView && agents.isPending)
  const total = overview.data?.totals.total ?? 0
  const active = (overview.data?.totals.by_status.open ?? 0)
    + (overview.data?.totals.by_status.assigned ?? 0)
    + (overview.data?.totals.by_status.in_progress ?? 0)
    + (overview.data?.totals.by_status.waiting_for_customer ?? 0)
  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <PageHeader
          title="Analytics"
          description={`${isOrgView ? 'Organization-wide' : 'Your assigned-work'} performance, grounded in live support data.`}
          actions={(
            <div aria-label="Analytics date range" className="inline-flex rounded-xl border border-border-subtle bg-surface p-1">
              {WINDOWS.map((window) => (
                <Button key={window} size="sm" variant={days === window ? 'secondary' : 'ghost'} aria-pressed={days === window} onClick={() => setDays(window)}>
                  {window} days
                </Button>
              ))}
            </div>
          )}
        />

        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border-subtle bg-surface-sunken/50 px-4 py-2.5 text-xs text-ink-muted">
          <span>Showing {days} days of {isOrgView ? 'team' : 'assigned'} activity</span>
          <span className="tabular-nums">{overview.data ? `${new Date(overview.data.range.start).toLocaleDateString()} – ${new Date(overview.data.range.end).toLocaleDateString()}` : `Last ${days} days`}</span>
        </div>

        {allLoading ? <LoadingPanels /> : firstError ? (
          <ErrorState title="Analytics couldn’t load" error={firstError} onRetry={() => {
            void overview.refetch(); void tickets.refetch(); void sla.refetch(); void sentiment.refetch(); if (isOrgView) void agents.refetch()
          }} />
        ) : overview.data && tickets.data ? (
          <>
            <section aria-label="Performance summary" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <Metric label="Tickets created" value={total.toLocaleString()} detail={`Across the last ${days} days`} icon={<Ticket aria-hidden="true" className="size-4" />} />
              <Metric label="Active queue" value={active.toLocaleString()} detail={`${overview.data.totals.resolved} resolved · ${overview.data.totals.closed} closed in cohort`} icon={<ChartLineUp aria-hidden="true" className="size-4" />} tone="success" />
              <Metric label="First response" value={formatDuration(overview.data.response_time.average_seconds)} detail={`${overview.data.response_time.count} tickets received a reply`} icon={<Clock aria-hidden="true" className="size-4" />} tone="warning" />
              <Metric label="AI usage" value={formatCost(overview.data.ai_usage.cost_usd)} detail={`${overview.data.ai_usage.calls} calls · ${overview.data.ai_usage.failed_calls} failed`} icon={<CurrencyDollar aria-hidden="true" className="size-4" />} tone="brand" />
            </section>

            <Card className="overflow-hidden">
              <PanelHeading title="Ticket volume" detail="Tickets created each day. Empty days are shown as zero." />
              <div className="h-72 px-3 py-5 sm:px-5" role="img" aria-label={`Daily ticket volume for the last ${days} days`}>
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                    <defs><linearGradient id="volumeFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--color-brand)" stopOpacity={0.22} /><stop offset="95%" stopColor="var(--color-brand)" stopOpacity={0.01} /></linearGradient></defs>
                    <CartesianGrid vertical={false} stroke="var(--color-border-subtle)" strokeDasharray="3 5" />
                    <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={26} tick={{ fill: 'var(--color-ink-subtle)', fontSize: 11 }} />
                    <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fill: 'var(--color-ink-subtle)', fontSize: 11 }} />
                    <Tooltip contentStyle={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: 12, color: 'var(--color-ink)', fontSize: 12 }} labelStyle={{ color: 'var(--color-ink-muted)' }} />
                    <Area type="monotone" dataKey="count" name="Tickets" stroke="var(--color-brand)" strokeWidth={2.5} fill="url(#volumeFill)" activeDot={{ r: 4, strokeWidth: 0 }} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </Card>

            <div className="grid items-start gap-5 lg:grid-cols-2">
              <Card className="overflow-hidden">
                <PanelHeading title="Priority mix" detail="Tickets created in the selected window." />
                <div className="h-64 px-4 py-5">
                  {tickets.data.by_priority.length ? <ResponsiveContainer width="100%" height="100%"><BarChart data={tickets.data.by_priority} layout="vertical" margin={{ top: 0, right: 22, bottom: 0, left: 0 }}>
                    <CartesianGrid horizontal={false} stroke="var(--color-border-subtle)" />
                    <XAxis type="number" allowDecimals={false} tickLine={false} axisLine={false} tick={{ fill: 'var(--color-ink-subtle)', fontSize: 11 }} />
                    <YAxis type="category" dataKey="priority" width={72} tickLine={false} axisLine={false} tick={{ fill: 'var(--color-ink-muted)', fontSize: 11 }} />
                    <Tooltip contentStyle={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: 12, color: 'var(--color-ink)', fontSize: 12 }} />
                    <Bar dataKey="count" name="Tickets" radius={[0, 5, 5, 0]} barSize={20}>{tickets.data.by_priority.map((entry) => <Cell key={entry.priority} fill={PRIORITY_COLORS[entry.priority] ?? 'var(--color-brand)'} />)}</Bar>
                  </BarChart></ResponsiveContainer> : <EmptyState title="No priority data" />}
                </div>
              </Card>

              <Card className="overflow-hidden">
                <PanelHeading title="SLA health" detail="Compliance is measured for tickets created in the selected window." />
                {sla.data ? <div className="p-5">
                  <div className="flex items-end justify-between gap-4">
                    <div><p className="text-xs text-ink-muted">Overall compliance</p><p className="mt-2 text-4xl font-semibold tracking-[-0.05em] tabular-nums text-ink">{sla.data.compliance.rate === null ? '—' : `${(sla.data.compliance.rate * 100).toFixed(1)}%`}</p></div>
                    <Badge variant={sla.data.overdue ? 'danger' : 'success'} dot>{sla.data.overdue ? `${sla.data.overdue} overdue` : 'No overdue tickets'}</Badge>
                  </div>
                  <div className="mt-5 grid grid-cols-2 gap-3">
                    <div className="rounded-lg bg-surface-sunken p-3"><p className="text-xs text-ink-muted">Response met</p><p className="mt-1 text-lg font-semibold tabular-nums text-ink">{sla.data.compliance.response.met.toLocaleString()}</p><p className="text-[0.6875rem] text-ink-subtle">{sla.data.compliance.response.breached} breached</p></div>
                    <div className="rounded-lg bg-surface-sunken p-3"><p className="text-xs text-ink-muted">Resolution met</p><p className="mt-1 text-lg font-semibold tabular-nums text-ink">{sla.data.compliance.resolution.met.toLocaleString()}</p><p className="text-[0.6875rem] text-ink-subtle">{sla.data.compliance.resolution.breached} breached</p></div>
                  </div>
                  {sla.data.risks.length > 0 && <div className="mt-5 border-t border-border-subtle pt-4"><div className="mb-2 flex items-center gap-2 text-xs font-semibold text-ink"><Warning aria-hidden="true" className="size-4 text-warning-text" />Needs attention now</div><div className="divide-y divide-border-subtle">{sla.data.risks.slice(0, 4).map((risk) => <Link key={risk.id} to={`/tickets/${risk.id}`} className="flex items-center gap-3 py-2.5 text-xs hover:text-brand-text"><span className="font-mono text-ink-subtle">#{risk.number}</span><span className="min-w-0 flex-1 truncate text-ink">{risk.subject}</span><Badge variant={risk.state === 'breached' ? 'danger' : 'warning'} size="sm">{risk.state === 'breached' ? 'Breached' : `${Math.ceil(Math.max(0, risk.remaining_seconds) / 60)}m`}</Badge></Link>)}</div></div>}
                </div> : <div className="p-5"><p className="text-sm text-ink-muted">SLA metrics are not available for this role.</p></div>}
              </Card>
            </div>

            <div className="grid items-start gap-5 lg:grid-cols-2">
              <Card className="overflow-hidden">
                <PanelHeading title="Top categories" detail="AI classified categories from the selected window." />
                <div className="divide-y divide-border-subtle">
                  {tickets.data.by_category.length ? tickets.data.by_category.slice(0, 6).map((item) => <div key={item.category ?? 'uncategorized'} className="flex items-center gap-3 px-5 py-3"><span className="min-w-0 flex-1 truncate text-sm text-ink">{item.category ?? 'Uncategorized'}</span><span className="h-1.5 w-24 overflow-hidden rounded-full bg-surface-sunken"><span className="block h-full rounded-full bg-brand" style={{ width: `${Math.max(5, (item.count / Math.max(1, ...tickets.data.by_category.map((category) => category.count))) * 100)}%` }} /></span><span className="w-8 text-right text-xs tabular-nums text-ink-muted">{item.count}</span></div>) : <EmptyState title="No category data yet" description="Categories appear as tickets are classified." />}
                </div>
              </Card>

              <Card className="overflow-hidden">
                <PanelHeading title="Customer sentiment" detail="Classification coverage for tickets created in the selected window." />
                {sentiment.data ? <div className="p-5">
                  <div className="mb-4 flex items-baseline justify-between"><span className="text-3xl font-semibold tracking-[-0.04em] tabular-nums text-ink">{sentiment.data.analysed}</span><span className="text-xs text-ink-muted">of {sentiment.data.analysed + sentiment.data.unanalysed} tickets analysed</span></div>
                  <div className="flex h-2 overflow-hidden rounded-full bg-surface-sunken">{sentiment.data.buckets.filter((item) => item.sentiment).map((item) => <span key={item.sentiment} title={`${item.sentiment}: ${item.count}`} className={item.sentiment === 'positive' ? 'bg-success' : item.sentiment === 'negative' ? 'bg-danger' : 'bg-warning'} style={{ width: `${sentiment.data.analysed ? (item.count / sentiment.data.analysed) * 100 : 0}%` }} />)}</div>
                  <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2">{sentiment.data.buckets.filter((item) => item.sentiment).map((item) => <div key={item.sentiment} className="flex items-center gap-2 text-xs capitalize text-ink-muted"><span className={`size-2 rounded-full ${item.sentiment === 'positive' ? 'bg-success' : item.sentiment === 'negative' ? 'bg-danger' : 'bg-warning'}`} />{item.sentiment}<strong className="font-medium tabular-nums text-ink">{item.count}</strong></div>)}</div>
                  {sentiment.data.analysed === 0 && <p className="mt-4 text-xs text-ink-subtle">Sentiment insights will populate as tickets receive AI analysis.</p>}
                </div> : null}
              </Card>
            </div>

            {isOrgView && agents.data && <Card className="overflow-hidden">
              <PanelHeading title="Team workload" detail={`${agents.data.total} agents and unassigned work in this window.`} />
              {agents.data.agents.length ? <div className="overflow-x-auto"><table className="w-full min-w-[38rem] text-left text-sm"><thead className="bg-surface-sunken/60 text-[0.6875rem] uppercase tracking-wide text-ink-subtle"><tr><th className="px-5 py-3 font-medium">Agent</th><th className="px-4 py-3 text-right font-medium">Assigned</th><th className="px-4 py-3 text-right font-medium">Open</th><th className="px-4 py-3 text-right font-medium">Resolved</th><th className="px-5 py-3 text-right font-medium">Avg. resolution</th></tr></thead><tbody className="divide-y divide-border-subtle">{agents.data.agents.map((agent) => <tr key={agent.agent_id ?? 'unassigned'}><td className="px-5 py-3.5 font-medium text-ink">{agent.agent_name ?? 'Unassigned'}</td><td className="px-4 py-3.5 text-right tabular-nums text-ink-muted">{agent.assigned}</td><td className="px-4 py-3.5 text-right tabular-nums text-ink-muted">{agent.open}</td><td className="px-4 py-3.5 text-right tabular-nums text-ink-muted">{agent.resolved}</td><td className="px-5 py-3.5 text-right tabular-nums text-ink-muted">{formatDuration(agent.average_resolution_seconds)}</td></tr>)}</tbody></table></div> : <EmptyState title="No team activity in this window" />}
            </Card>}

            <div className="flex items-center justify-between gap-3 border-t border-border-subtle pt-4 text-xs text-ink-subtle">
              <span>Resolution time average: {formatDuration(overview.data.resolution_time.average_seconds)} across {overview.data.resolution_time.count} resolved tickets.</span>
              <Link to="/tickets" className="inline-flex shrink-0 items-center gap-1 font-medium text-brand-text hover:underline">Open tickets {isOrgView ? <ArrowUp aria-hidden="true" className="size-3.5 rotate-45" /> : <ArrowDown aria-hidden="true" className="size-3.5 rotate-45" />}</Link>
            </div>
          </>
        ) : null}
      </div>
    </AppShell>
  )
}
