import { ArrowClockwise, ArrowRight, CalendarBlank, Funnel, MagnifyingGlass, Scroll, Ticket, UserCircle } from '@phosphor-icons/react'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Input } from '@/components/ui/Input'
import { Select } from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { auditKeys, type AuditFilters } from '@/features/audit/keys'
import { can, PERMISSIONS } from '@/features/auth/permissions'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { teamKeys } from '@/features/team/keys'
import { useSessionStore } from '@/store/auth'
import type { components } from '@/types/api'

type AuditEntry = components['schemas']['AuditLogRead']
type AuditAction = components['schemas']['AuditAction']
type UserRead = components['schemas']['UserRead']
const PAGE_SIZE = 50

const ACTION_LABELS: Record<AuditAction, string> = {
  user_created: 'User created',
  user_role_updated: 'User role updated',
  user_deactivated: 'User deactivated',
  password_reset: 'Password reset',
  email_verified: 'Email verified',
  ticket_created: 'Ticket created',
  ticket_assigned: 'Ticket assigned',
  ticket_priority_changed: 'Priority changed',
  ticket_status_changed: 'Status changed',
  ticket_resolved: 'Ticket resolved',
  ticket_reopened: 'Ticket reopened',
  knowledge_document_created: 'Knowledge source added',
  knowledge_document_deleted: 'Knowledge source deleted',
  ai_analysis_requested: 'AI analysis requested',
  ai_response_accepted: 'AI response accepted',
  ai_response_regenerated: 'AI response regenerated',
  sla_policy_updated: 'SLA policy updated',
  organization_updated: 'Organization updated',
}

const ACTIONS = Object.keys(ACTION_LABELS) as AuditAction[]
const TARGET_TYPES = ['ticket', 'user', 'knowledge_document', 'sla_policy', 'organization']

function localDate(daysAgo = 0): string {
  const date = new Date()
  date.setDate(date.getDate() - daysAgo)
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 10)
}

function dateParam(value: string, end = false): string | undefined {
  if (!value) return undefined
  const date = new Date(`${value}T00:00:00`)
  if (end) date.setDate(date.getDate() + 1)
  return date.toISOString()
}

function actionVariant(action: AuditAction): 'success' | 'warning' | 'danger' | 'brand' | 'neutral' {
  if (action.includes('deleted') || action.includes('deactivated')) return 'danger'
  if (action.includes('created') || action.includes('resolved') || action.includes('accepted')) return 'success'
  if (action.includes('updated') || action.includes('changed') || action.includes('assigned')) return 'brand'
  if (action.includes('requested') || action.includes('regenerated')) return 'warning'
  return 'neutral'
}

function summaryText(entry: AuditEntry): string {
  const target = entry.extra_data.target_name ?? entry.extra_data.title ?? entry.extra_data.subject
  if (typeof target === 'string' && target.trim()) return target
  if (entry.target_type === 'ticket' && entry.target_id) return `Ticket ${entry.target_id.slice(0, 8)}`
  if (entry.target_type === 'user' && entry.target_id) return `User ${entry.target_id.slice(0, 8)}`
  return entry.target_type.replaceAll('_', ' ')
}

function safeMetadata(entry: AuditEntry): [string, string][] {
  return Object.entries(entry.extra_data)
    .filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value))
    .map(([key, value]): [string, string] => [key.replaceAll('_', ' '), String(value)])
    .slice(0, 8)
}

function targetUrl(entry: AuditEntry): string | undefined {
  if (!entry.target_id) return undefined
  if (entry.target_type === 'ticket') return `/tickets/${entry.target_id}`
  if (entry.target_type === 'knowledge_document') return `/knowledge#document-${entry.target_id}`
  if (entry.target_type === 'sla_policy') return '/sla'
  return undefined
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  const detail = safeMetadata(entry)
  const href = targetUrl(entry)
  return (
    <article className="relative grid grid-cols-[2rem_minmax(0,1fr)] gap-3 pb-5 last:pb-0">
      <div className="relative flex justify-center"><span className="absolute top-8 bottom-[-1.25rem] w-px bg-border-subtle last:hidden" /><span className="z-10 grid size-8 place-items-center rounded-full border border-border-subtle bg-surface text-ink-muted">{entry.target_type === 'ticket' ? <Ticket aria-hidden="true" className="size-3.5" /> : entry.target_type === 'user' ? <UserCircle aria-hidden="true" className="size-3.5" /> : <Scroll aria-hidden="true" className="size-3.5" />}</span></div>
      <div className="min-w-0 rounded-xl border border-border-subtle bg-surface px-4 py-3.5">
        <div className="flex flex-wrap items-center gap-2"><Badge variant={actionVariant(entry.action)} size="sm">{ACTION_LABELS[entry.action]}</Badge><time className="ml-auto text-[0.6875rem] tabular-nums text-ink-subtle" dateTime={entry.created_at}>{new Date(entry.created_at).toLocaleString()}</time></div>
        <div className="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm"><span className="font-medium text-ink">{entry.actor_email ?? 'System'}</span><span className="text-ink-subtle">·</span>{href ? <Link to={href} className="min-w-0 truncate text-ink-muted hover:text-brand-text hover:underline">{summaryText(entry)}</Link> : <span className="min-w-0 truncate text-ink-muted">{summaryText(entry)}</span>}</div>
        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.6875rem] text-ink-subtle"><span className="capitalize">{entry.target_type.replaceAll('_', ' ')}</span>{entry.target_id && <><span>·</span><span className="font-mono">{entry.target_id}</span></>}</div>
        {(detail.length > 0 || entry.ip_address || entry.user_agent) && <details className="mt-3 border-t border-border-subtle pt-2.5"><summary className="cursor-pointer list-none text-[0.6875rem] font-medium text-ink-subtle marker:hidden">View event details</summary><dl className="mt-2 grid gap-x-4 gap-y-2 sm:grid-cols-2">{detail.map(([key, value]) => <div key={key} className="min-w-0"><dt className="text-[0.625rem] capitalize text-ink-subtle">{key}</dt><dd className="break-words text-xs text-ink-muted">{value}</dd></div>)}{entry.ip_address && <div><dt className="text-[0.625rem] text-ink-subtle">IP address</dt><dd className="font-mono text-xs text-ink-muted">{entry.ip_address}</dd></div>}{entry.user_agent && <div className="sm:col-span-2"><dt className="text-[0.625rem] text-ink-subtle">User agent</dt><dd className="break-all text-xs text-ink-muted">{entry.user_agent}</dd></div>}</dl></details>}
      </div>
    </article>
  )
}

export function AuditPage() {
  const user = useSessionStore((state) => state.user)
  const [searchParams] = useSearchParams()
  const [action, setAction] = useState('')
  const [actorId, setActorId] = useState('')
  const [targetType, setTargetType] = useState(() => searchParams.get('target_type') ?? '')
  const [targetId, setTargetId] = useState(() => searchParams.get('target_id') ?? '')
  const [startDate, setStartDate] = useState(() => searchParams.has('target_id') ? '' : localDate(30))
  const [endDate, setEndDate] = useState(() => searchParams.has('target_id') ? '' : localDate())
  const [offset, setOffset] = useState(0)
  const allowed = can(user?.role, PERMISSIONS.AUDIT_VIEW)
  const filters = useMemo<AuditFilters>(() => {
    const createdAfter = dateParam(startDate)
    const createdBefore = dateParam(endDate, true)
    return {
      limit: PAGE_SIZE,
      offset,
      ...(action ? { action } : {}),
      ...(actorId ? { actor_user_id: actorId } : {}),
      ...(targetType ? { target_type: targetType } : {}),
      ...(targetId.trim() ? { target_id: targetId.trim() } : {}),
      ...(createdAfter ? { created_after: createdAfter } : {}),
      ...(createdBefore ? { created_before: createdBefore } : {}),
    }
  }, [action, actorId, endDate, offset, startDate, targetId, targetType])

  const entries = useQuery({
    queryKey: auditKeys.list(filters),
    queryFn: async () => (await apiClient.get<AuditEntry[]>('/audit-logs', { params: filters })).data,
    enabled: Boolean(user && allowed),
  })
  const users = useQuery({
    queryKey: teamKeys.list({ limit: 100, offset: 0 }),
    queryFn: async () => (await apiClient.get<UserRead[]>('/users', { params: { limit: 100, offset: 0 } })).data,
    enabled: Boolean(user && allowed),
  })

  if (!user) return null
  const hasFilters = Boolean(action || actorId || targetType || targetId || startDate || endDate)
  const changeFilter = (setter: (value: string) => void, value: string) => {
    setter(value)
    setOffset(0)
  }

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <PageHeader title="Audit log" description="A chronological, read-only record of important actions in your organization." actions={<Badge variant="info" dot>Admin only</Badge>} />

        <Card className="overflow-hidden">
          <div className="flex items-center gap-2 border-b border-border-subtle px-4 py-3.5"><Funnel aria-hidden="true" className="size-4 text-ink-subtle" /><h2 className="text-xs font-semibold text-ink">Filter events</h2><Button variant="ghost" size="sm" className="ml-auto" leadingIcon={<ArrowClockwise aria-hidden="true" />} disabled={!hasFilters} onClick={() => { setAction(''); setActorId(''); setTargetType(''); setTargetId(''); setStartDate(''); setEndDate(''); setOffset(0) }}>Clear</Button></div>
          <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
            <label className="space-y-1.5"><span className="text-[0.6875rem] font-medium text-ink-muted">Action</span><Select value={action} placeholder="All actions" onChange={(event) => changeFilter(setAction, event.currentTarget.value)}>{ACTIONS.map((value) => <option key={value} value={value}>{ACTION_LABELS[value]}</option>)}</Select></label>
            <label className="space-y-1.5"><span className="text-[0.6875rem] font-medium text-ink-muted">Actor</span><Select value={actorId} placeholder="Anyone" onChange={(event) => changeFilter(setActorId, event.currentTarget.value)}>{users.data?.map((member) => <option key={member.id} value={member.id}>{member.name} · {member.email}</option>)}</Select></label>
            <label className="space-y-1.5"><span className="text-[0.6875rem] font-medium text-ink-muted">Target</span><Select value={targetType} placeholder="Any target" onChange={(event) => changeFilter(setTargetType, event.currentTarget.value)}>{TARGET_TYPES.map((value) => <option key={value} value={value}>{value.replaceAll('_', ' ')}</option>)}</Select></label>
            <label className="space-y-1.5"><span className="text-[0.6875rem] font-medium text-ink-muted">Target ID</span><Input value={targetId} maxLength={36} placeholder="Entity ID" onChange={(event) => changeFilter(setTargetId, event.currentTarget.value)} /></label>
            <label className="space-y-1.5"><span className="text-[0.6875rem] font-medium text-ink-muted">From</span><Input type="date" value={startDate} max={endDate || undefined} onChange={(event) => changeFilter(setStartDate, event.currentTarget.value)} leadingIcon={<CalendarBlank aria-hidden="true" />} /></label>
            <label className="space-y-1.5"><span className="text-[0.6875rem] font-medium text-ink-muted">Through</span><Input type="date" value={endDate} min={startDate || undefined} onChange={(event) => changeFilter(setEndDate, event.currentTarget.value)} leadingIcon={<CalendarBlank aria-hidden="true" />} /></label>
          </div>
        </Card>

        <section aria-labelledby="activity-heading">
          <div className="mb-3 flex items-center justify-between gap-3"><div><h2 id="activity-heading" className="text-sm font-semibold text-ink">Activity</h2><p className="mt-1 text-xs text-ink-muted">Newest events first · showing up to {PAGE_SIZE} per page</p></div><Badge variant="neutral">Page {Math.floor(offset / PAGE_SIZE) + 1}</Badge></div>
          {entries.isPending ? <div className="space-y-3">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-24 rounded-xl" />)}</div> : entries.error ? <ErrorState title="Audit activity couldn’t load" error={entries.error} onRetry={() => { void entries.refetch() }} /> : entries.data?.length ? <div className="space-y-0.5">{entries.data.map((entry) => <AuditRow key={entry.id} entry={entry} />)}</div> : <Card><EmptyState icon={<MagnifyingGlass aria-hidden="true" />} title="No events match these filters" description="Try widening the date range or clearing one of the filters." action={<Button size="sm" variant="secondary" onClick={() => { setAction(''); setActorId(''); setTargetType(''); setTargetId(''); setStartDate(''); setEndDate(''); setOffset(0) }}>Clear filters</Button>} /></Card>}
          {!entries.isPending && !entries.error && entries.data && <div className="mt-4 flex items-center justify-between border-t border-border-subtle pt-4"><Button size="sm" variant="secondary" disabled={offset === 0} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>Previous</Button><span className="text-xs tabular-nums text-ink-subtle">{entries.data.length} events</span><Button size="sm" variant="secondary" disabled={entries.data.length < PAGE_SIZE} onClick={() => setOffset((value) => value + PAGE_SIZE)}>Next</Button></div>}
        </section>

        <div className="flex items-center justify-between gap-3 border-t border-border-subtle pt-4 text-xs text-ink-subtle"><span>Audit entries are immutable and written by the backend when an action occurs.</span><Link to="/users" className="inline-flex shrink-0 items-center gap-1 font-medium text-brand-text hover:underline">Team directory <ArrowRight aria-hidden="true" className="size-3.5" /></Link></div>
      </div>
    </AppShell>
  )
}
