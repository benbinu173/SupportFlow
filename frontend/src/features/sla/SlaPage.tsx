import { ArrowRight, Check, Clock, ShieldCheck, SlidersHorizontal, Warning } from '@phosphor-icons/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { Dialog } from '@/components/ui/Dialog'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { analyticsKeys } from '@/features/analytics/keys'
import { can, PERMISSIONS } from '@/features/auth/permissions'
import { auditHistoryUrl } from '@/features/audit/keys'
import { slaKeys } from '@/features/sla/keys'
import { ticketKeys } from '@/features/tickets/keys'
import { TICKET_STATUS_LABELS } from '@/features/tickets/presentation'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { toApiError } from '@/services/api/errors'
import { toast } from '@/store/toasts'
import { useSessionStore } from '@/store/auth'
import type { components } from '@/types/api'

type Policy = components['schemas']['SLAPolicyRead']
type PolicyUpdate = components['schemas']['SLAPolicyUpdate']
type Priority = components['schemas']['TicketPriority']
type SLAOverview = components['schemas']['AnalyticsSLA']

const PRIORITY_STYLE: Record<Priority, { label: string; variant: 'danger' | 'warning' | 'brand' | 'neutral'; color: string }> = {
  urgent: { label: 'Urgent', variant: 'danger', color: 'bg-danger' },
  high: { label: 'High', variant: 'warning', color: 'bg-warning' },
  medium: { label: 'Medium', variant: 'brand', color: 'bg-brand' },
  low: { label: 'Low', variant: 'neutral', color: 'bg-ink-subtle' },
}

type Draft = {
  response_time_minutes: string
  resolution_time_minutes: string
  warning_threshold_percent: string
  is_active: boolean
}

function asDraft(policy: Policy): Draft {
  return {
    response_time_minutes: String(policy.response_time_minutes),
    resolution_time_minutes: String(policy.resolution_time_minutes),
    warning_threshold_percent: String(policy.warning_threshold_percent),
    is_active: policy.is_active,
  }
}

function duration(seconds: number): string {
  const value = Math.abs(Math.floor(seconds))
  if (value >= 86_400) return `${Math.floor(value / 86_400)}d ${Math.floor((value % 86_400) / 3_600)}h`
  if (value >= 3_600) return `${Math.floor(value / 3_600)}h ${Math.floor((value % 3_600) / 60)}m`
  return `${Math.max(1, Math.floor(value / 60))}m`
}

function target(minutes: number): string {
  if (minutes % 1440 === 0) return `${minutes / 1440} ${minutes === 1440 ? 'day' : 'days'}`
  if (minutes % 60 === 0) return `${minutes / 60} ${minutes === 60 ? 'hour' : 'hours'}`
  return `${minutes} min`
}

function PolicyEditor({
  policy,
  draft,
  setDraft,
  onClose,
  onSave,
  saving,
  error,
}: {
  policy: Policy | null
  draft: Draft | null
  setDraft: (draft: Draft) => void
  onClose: () => void
  onSave: (event: FormEvent<HTMLFormElement>) => void
  saving: boolean
  error?: string | undefined
}) {
  const priority = policy ? PRIORITY_STYLE[policy.priority] : null
  return (
    <Dialog
      open={Boolean(policy && draft)}
      onClose={onClose}
      title={policy ? `Edit ${priority?.label.toLowerCase()} SLA` : 'Edit SLA'}
      description="These targets define when the service warns the team and when a ticket breaches its commitment."
      footer={(
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button form="sla-policy-form" type="submit" loading={saving} leadingIcon={<Check aria-hidden="true" />}>Save policy</Button>
        </>
      )}
    >
      {policy && draft && <form id="sla-policy-form" onSubmit={onSave} className="space-y-4">
        <div className="flex items-center gap-3 rounded-xl border border-border-subtle bg-surface-sunken p-3">
          <span className={`size-2.5 rounded-full ${priority?.color}`} />
          <div className="flex-1"><p className="text-sm font-medium text-ink">{priority?.label} priority</p><p className="text-xs text-ink-muted">Applies to matching tickets across the organization</p></div>
          <label className="flex items-center gap-2 text-xs font-medium text-ink"><input type="checkbox" checked={draft.is_active} onChange={(event) => setDraft({ ...draft, is_active: event.currentTarget.checked })} className="size-4 rounded border-border accent-brand" />Active</label>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="space-y-1.5"><span className="text-xs font-medium text-ink">First response target</span><Input required type="number" min={1} max={525600} value={draft.response_time_minutes} onChange={(event) => setDraft({ ...draft, response_time_minutes: event.currentTarget.value })} /><span className="block text-[0.6875rem] text-ink-subtle">Minutes until a first public reply is due.</span></label>
          <label className="space-y-1.5"><span className="text-xs font-medium text-ink">Resolution target</span><Input required type="number" min={1} max={525600} value={draft.resolution_time_minutes} onChange={(event) => setDraft({ ...draft, resolution_time_minutes: event.currentTarget.value })} /><span className="block text-[0.6875rem] text-ink-subtle">Must be at least as long as the response target.</span></label>
        </div>

        <label className="block max-w-xs space-y-1.5"><span className="text-xs font-medium text-ink">Warn when target is used</span><div className="relative"><Input required type="number" min={1} max={99} value={draft.warning_threshold_percent} onChange={(event) => setDraft({ ...draft, warning_threshold_percent: event.currentTarget.value })} className="pr-9" /><span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm text-ink-subtle">%</span></div><span className="block text-[0.6875rem] text-ink-subtle">Choose a value from 1 to 99 percent.</span></label>

        {error && <p role="alert" className="rounded-lg border border-danger-border bg-danger-subtle px-3 py-2 text-xs text-danger-text">{error}</p>}
      </form>}
    </Dialog>
  )
}

export function SlaPage() {
  const user = useSessionStore((state) => state.user)
  const queryClient = useQueryClient()
  const canConfigure = can(user?.role, PERMISSIONS.SLA_CONFIGURE)
  const canViewAudit = can(user?.role, PERMISSIONS.AUDIT_VIEW)
  const [selectedPolicy, setSelectedPolicy] = useState<Policy | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [formError, setFormError] = useState<string>()

  const policies = useQuery({
    queryKey: slaKeys.policies(),
    queryFn: async () => (await apiClient.get<Policy[]>('/sla/policies')).data,
    enabled: Boolean(user && can(user.role, PERMISSIONS.SLA_VIEW)),
  })
  const overview = useQuery({
    queryKey: analyticsKeys.sla(),
    queryFn: async () => (await apiClient.get<SLAOverview>('/analytics/sla', { params: { limit: 8 } })).data,
    enabled: Boolean(user && can(user.role, PERMISSIONS.ANALYTICS_OWN)),
    refetchInterval: 30_000,
  })

  const updatePolicy = useMutation({
    mutationFn: async ({ priority, payload }: { priority: Priority; payload: PolicyUpdate }) =>
      (await apiClient.patch<Policy>(`/sla/policies/${priority}`, payload)).data,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: slaKeys.policies() }),
        queryClient.invalidateQueries({ queryKey: analyticsKeys.all }),
        queryClient.invalidateQueries({ queryKey: ticketKeys.all }),
      ])
      toast.success('SLA policy updated', 'New targets will apply to future ticket clock calculations.')
      setSelectedPolicy(null)
      setDraft(null)
      setFormError(undefined)
    },
  })

  const openEditor = (policy: Policy) => {
    setSelectedPolicy(policy)
    setDraft(asDraft(policy))
    setFormError(undefined)
  }

  const submitPolicy = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!selectedPolicy || !draft) return
    const response = Number(draft.response_time_minutes)
    const resolution = Number(draft.resolution_time_minutes)
    const warning = Number(draft.warning_threshold_percent)
    if (![response, resolution, warning].every(Number.isInteger) || response < 1 || response > 525600 || resolution < 1 || resolution > 525600 || warning < 1 || warning > 99) {
      setFormError('Enter whole numbers within the allowed ranges.')
      return
    }
    if (resolution < response) {
      setFormError('The resolution target must be at least as long as the response target.')
      return
    }
    setFormError(undefined)
    updatePolicy.mutate({
      priority: selectedPolicy.priority,
      payload: {
        response_time_minutes: response,
        resolution_time_minutes: resolution,
        warning_threshold_percent: warning,
        is_active: draft.is_active,
      },
    })
  }

  if (!user) return null
  const loadError = policies.error ?? overview.error

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <PageHeader
          title="SLA management"
          description="Service targets, compliance, and tickets approaching their deadlines."
          actions={canConfigure ? <Badge variant="info" dot>Admin controls enabled</Badge> : <Badge variant="neutral">Read only</Badge>}
        />

        {loadError ? <ErrorState title="SLA information couldn’t load" error={loadError} onRetry={() => { void policies.refetch(); void overview.refetch() }} /> : (
          <>
            <section aria-label="Current SLA status" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {overview.isPending ? Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-28 rounded-xl" />) : <>
                <Card><div className="p-4"><p className="text-xs text-ink-muted">Active tickets</p><p className="mt-2 text-2xl font-semibold tabular-nums text-ink">{overview.data?.open_tickets ?? '—'}</p><p className="mt-1 text-[0.6875rem] text-ink-subtle">Currently covered by an SLA policy</p></div></Card>
                <Card><div className="p-4"><p className="text-xs text-ink-muted">Past due</p><p className={`mt-2 text-2xl font-semibold tabular-nums ${overview.data?.overdue ? 'text-danger-text' : 'text-ink'}`}>{overview.data?.overdue ?? '—'}</p><p className="mt-1 text-[0.6875rem] text-ink-subtle">Across the live queue</p></div></Card>
                <Card><div className="p-4"><p className="text-xs text-ink-muted">Targets met</p><p className="mt-2 text-2xl font-semibold tabular-nums text-ink">{overview.data?.compliance.rate == null ? '—' : `${(overview.data.compliance.rate * 100).toFixed(1)}%`}</p><p className="mt-1 text-[0.6875rem] text-ink-subtle">For tickets created in the last 30 days</p></div></Card>
                <Card><div className="p-4"><p className="text-xs text-ink-muted">Breached targets</p><p className="mt-2 text-2xl font-semibold tabular-nums text-ink">{overview.data?.compliance.breached ?? '—'}</p><p className="mt-1 text-[0.6875rem] text-ink-subtle">Response and resolution combined</p></div></Card>
              </>}
            </section>

            <section aria-labelledby="policy-heading" className="space-y-3">
              <div className="flex flex-wrap items-end justify-between gap-3"><div><h2 id="policy-heading" className="text-base font-semibold text-ink">Priority policies</h2><p className="mt-1 text-xs text-ink-muted">The clock each new ticket receives based on its priority.</p></div>{canConfigure && <span className="inline-flex items-center gap-1.5 text-xs text-ink-subtle"><SlidersHorizontal aria-hidden="true" className="size-3.5" />Changes are written to the audit log</span>}</div>

              {policies.isPending ? <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-48 rounded-xl" />)}</div> : policies.data && (
                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                  {policies.data.map((policy) => {
                    const style = PRIORITY_STYLE[policy.priority]
                    return <Card key={policy.id} className="overflow-hidden">
                      <div className={`h-1 ${style.color}`} />
                      <div className="p-4">
                        <div className="flex items-center justify-between gap-2"><Badge variant={style.variant} dot>{style.label}</Badge><Badge variant={policy.is_active ? 'success' : 'neutral'}>{policy.is_active ? 'Active' : 'Paused'}</Badge></div>
                        <dl className="mt-5 space-y-3"><div className="flex justify-between gap-2"><dt className="text-xs text-ink-muted">First response</dt><dd className="text-xs font-semibold tabular-nums text-ink">{target(policy.response_time_minutes)}</dd></div><div className="flex justify-between gap-2"><dt className="text-xs text-ink-muted">Resolution</dt><dd className="text-xs font-semibold tabular-nums text-ink">{target(policy.resolution_time_minutes)}</dd></div><div className="flex justify-between gap-2"><dt className="text-xs text-ink-muted">Warning at</dt><dd className="text-xs font-semibold tabular-nums text-ink">{policy.warning_threshold_percent}%</dd></div></dl>
                        <div className="mt-5 flex items-center gap-2">{canConfigure && <Button variant="secondary" size="sm" className="min-w-0 flex-1" onClick={() => openEditor(policy)}>Edit policy</Button>}{canViewAudit && <Link to={auditHistoryUrl('sla_policy', policy.id)} className="rounded-lg px-3 py-2 text-xs font-medium text-ink-subtle hover:bg-surface-sunken hover:text-brand-text">History</Link>}</div>
                      </div>
                    </Card>
                  })}
                </div>
              )}
            </section>

            <Card className="overflow-hidden">
              <div className="flex items-center justify-between gap-3 border-b border-border-subtle px-5 py-4"><div><h2 className="text-sm font-semibold text-ink">At-risk tickets</h2><p className="mt-1 text-xs text-ink-muted">Nearest active SLA deadlines, updated every 30 seconds.</p></div><Badge variant={overview.data?.overdue ? 'danger' : 'success'} dot>{overview.data?.overdue ? `${overview.data.overdue} overdue` : 'No overdue tickets'}</Badge></div>
              {overview.isPending ? <div className="space-y-3 p-5">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-12 rounded-lg" />)}</div> : overview.data?.risks.length ? <div className="divide-y divide-border-subtle">{overview.data.risks.map((risk) => {
                const priority = PRIORITY_STYLE[risk.priority]
                const breached = risk.state === 'breached'
                return <Link key={risk.id} to={`/tickets/${risk.id}`} className="group flex items-center gap-3 px-4 py-4 transition-colors hover:bg-surface-sunken/50 sm:px-5"><span className={`grid size-9 shrink-0 place-items-center rounded-xl ${breached ? 'bg-danger-subtle text-danger-text' : 'bg-warning-subtle text-warning-text'}`}>{breached ? <Warning aria-hidden="true" className="size-4" /> : <Clock aria-hidden="true" className="size-4" />}</span><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium text-ink group-hover:text-brand-text">{risk.subject}</p><p className="mt-1 flex items-center gap-2 text-[0.6875rem] text-ink-subtle"><span className="font-mono">#{risk.number}</span><span>·</span><span>{TICKET_STATUS_LABELS[risk.status]}</span><span>·</span><span className="capitalize">{priority.label}</span></p></div><Badge variant={breached ? 'danger' : priority.variant} size="sm">{breached ? 'Breached' : `${duration(risk.remaining_seconds)} left`}</Badge><ArrowRight aria-hidden="true" className="size-4 shrink-0 text-ink-subtle group-hover:text-brand-text" /></Link>
              })}</div> : <EmptyState icon={<ShieldCheck aria-hidden="true" />} title="No tickets need attention" description="Tickets nearing an SLA deadline will appear here." />}
            </Card>

            <div className="flex items-center justify-between gap-4 border-t border-border-subtle pt-4 text-xs text-ink-subtle"><span>Paused policies stay visible and can be reactivated by an admin.</span><Link to="/analytics" className="inline-flex items-center gap-1 font-medium text-brand-text hover:underline">View full analytics <ArrowRight aria-hidden="true" className="size-3.5" /></Link></div>
          </>
        )}
      </div>

      <PolicyEditor
        policy={selectedPolicy}
        draft={draft}
        setDraft={setDraft}
        onClose={() => { if (!updatePolicy.isPending) { setSelectedPolicy(null); setDraft(null); setFormError(undefined) } }}
        onSave={submitPolicy}
        saving={updatePolicy.isPending}
        error={formError ?? (updatePolicy.error ? toApiError(updatePolicy.error).message : undefined)}
      />
    </AppShell>
  )
}
