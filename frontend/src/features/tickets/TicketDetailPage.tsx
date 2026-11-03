import {
  ArrowLeft,
  ArrowRight,
  CheckCircle,
  Clock,
  DownloadSimple,
  Paperclip,
  UploadSimple,
  UserCircle,
} from '@phosphor-icons/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { Avatar } from '@/components/ui/Avatar'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Dialog } from '@/components/ui/Dialog'
import { ErrorState } from '@/components/ui/States'
import { Select } from '@/components/ui/Select'
import { customerKeys } from '@/features/customers/keys'
import { can, isPortalRole, PERMISSIONS } from '@/features/auth/permissions'
import { teamKeys } from '@/features/team/keys'
import { ticketKeys } from '@/features/tickets/keys'
import { auditHistoryUrl } from '@/features/audit/keys'
import {
  TICKET_PRIORITY_VARIANTS,
  TICKET_STATUS_LABELS,
  TICKET_STATUS_VARIANTS,
  ticketPriorityLabel,
} from '@/features/tickets/presentation'
import { TicketActivity } from '@/features/tickets/TicketActivity'
import { TicketAssistant } from '@/features/tickets/TicketAssistant'
import { TicketConversation } from '@/features/tickets/TicketConversation'
import { apiClient } from '@/services/api/client'
import type { components } from '@/types/api'
import { toApiError } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import { toast } from '@/store/toasts'
import { cn } from '@/utils/cn'

type Ticket = components['schemas']['TicketRead']
type TicketStatus = components['schemas']['TicketStatus']
type TicketPriority = components['schemas']['TicketPriority']
type Message = components['schemas']['MessageRead']
type Customer = components['schemas']['CustomerRead']
type User = components['schemas']['UserRead']
type TicketEvent = components['schemas']['TicketEventRead']
type Attachment = components['schemas']['AttachmentRead']
type SLATimer = components['schemas']['SLATimerRead']

type TicketAction =
  | { kind: 'status'; status: TicketStatus }
  | { kind: 'priority'; priority: TicketPriority }
  | { kind: 'assignment'; assigned_agent_id: string | null }
  | { kind: 'close' }
  | { kind: 'reopen' }

type StatusAction = { label: string; status: TicketStatus; variant?: 'primary' | 'secondary' }

const STATUS_ACTIONS: Partial<Record<TicketStatus, StatusAction[]>> = {
  assigned: [{ label: 'Start working', status: 'in_progress', variant: 'primary' }],
  in_progress: [
    { label: 'Waiting for customer', status: 'waiting_for_customer', variant: 'secondary' },
    { label: 'Resolve ticket', status: 'resolved', variant: 'primary' },
  ],
  waiting_for_customer: [{ label: 'Resume work', status: 'in_progress', variant: 'primary' }],
}

function formatDate(value: string, options?: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(undefined, options ?? {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

function formatDuration(seconds: number): string {
  const absolute = Math.abs(Math.floor(seconds))
  const days = Math.floor(absolute / 86_400)
  const hours = Math.floor((absolute % 86_400) / 3_600)
  const minutes = Math.floor((absolute % 3_600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m`
  return `${absolute}s`
}

function formatBytes(bytes: number): string {
  if (bytes < 1_000) return `${bytes} B`
  if (bytes < 1_000_000) return `${(bytes / 1_000).toFixed(1)} KB`
  return `${(bytes / 1_000_000).toFixed(1)} MB`
}

function TicketSlaPanel({ ticket }: { ticket: Ticket }) {
  const [elapsedSeconds, setElapsedSeconds] = useState(0)

  useEffect(() => {
    const interval = setInterval(() => setElapsedSeconds((elapsed) => elapsed + 15), 15_000)
    return () => clearInterval(interval)
  }, [])

  if (!ticket.sla) return null

  const targetMinutes: Record<'response' | 'resolution', number> = {
    response: ticket.sla.policy.response_time_minutes,
    resolution: ticket.sla.policy.resolution_time_minutes,
  }
  const timers: Array<{ key: 'response' | 'resolution'; label: string; timer: SLATimer }> = [
    { key: 'response', label: 'First response', timer: ticket.sla.response },
    { key: 'resolution', label: 'Resolution', timer: ticket.sla.resolution },
  ]

  return (
    <Card variant="bezel" className="overflow-hidden">
      <div className="flex items-start justify-between gap-3 border-b border-border-subtle px-4 py-3.5">
        <div>
          <h2 className="text-sm font-semibold text-ink">Service targets</h2>
          <p className="mt-0.5 text-[0.6875rem] text-ink-muted">{ticketPriorityLabel(ticket.sla.policy.priority)} priority policy</p>
        </div>
        <Clock aria-hidden="true" className="mt-0.5 size-4 text-ink-subtle" />
      </div>

      <div className="flex flex-col divide-y divide-border-subtle px-4">
        {timers.map(({ key, label, timer }) => {
          const elapsedSinceRead = timer.stopped_at ? 0 : elapsedSeconds
          const remaining = timer.remaining_seconds - elapsedSinceRead
          const target = targetMinutes[key]
          const stateVariant =
            timer.state === 'met' ? 'success' : timer.state === 'breached' ? 'danger' : timer.state === 'warning' ? 'warning' : 'neutral'
          const description =
            timer.state === 'met'
              ? `Met · ${formatDuration(remaining)} to spare`
              : timer.state === 'breached'
                ? `Missed · ${formatDuration(remaining)} overdue`
                : remaining <= 0
                  ? `Overdue · ${formatDuration(remaining)}`
                  : `${formatDuration(remaining)} remaining`

          return (
            <div key={key} className="flex flex-col gap-1.5 py-3.5">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium text-ink">{label}</span>
                <Badge variant={stateVariant} size="sm" dot>
                  {timer.state === 'on_track' ? 'On track' : timer.state === 'warning' ? 'Warning' : timer.state === 'met' ? 'Met' : 'Breached'}
                </Badge>
              </div>
              <p className={cn(
                'text-xs tabular-nums',
                timer.state === 'breached' || remaining <= 0 ? 'font-semibold text-danger-text' :
                  timer.state === 'warning' ? 'font-medium text-warning-text' :
                    timer.state === 'met' ? 'font-medium text-success-text' : 'text-ink-muted',
              )}>
                {description}
              </p>
              <p className="text-[0.625rem] text-ink-subtle">
                {target >= 60 ? `${Math.floor(target / 60)}h${target % 60 ? ` ${target % 60}m` : ''}` : `${target}m`} target · due {formatDate(timer.due_at, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}
              </p>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

function CustomerPanel({ customer, loading }: { customer?: Customer | undefined; loading: boolean }) {
  return (
    <Card variant="bezel" className="overflow-hidden">
      <div className="border-b border-border-subtle px-4 py-3.5">
        <h2 className="text-sm font-semibold text-ink">Customer</h2>
      </div>
      {loading ? (
        <div className="flex items-center gap-3 p-4" aria-label="Loading customer details">
          <div className="size-10 animate-pulse rounded-full bg-surface-sunken" />
          <div className="flex flex-1 flex-col gap-2">
            <div className="h-3 w-1/2 animate-pulse rounded bg-surface-sunken" />
            <div className="h-3 w-3/4 animate-pulse rounded bg-surface-sunken" />
          </div>
        </div>
      ) : customer ? (
        <div className="flex flex-col gap-3 p-4">
          <div className="flex items-center gap-3">
            <Avatar name={customer.name} size="md" />
            <div className="flex min-w-0 flex-col gap-0.5">
              <p className="truncate text-sm font-semibold text-ink">{customer.name}</p>
              <a href={`mailto:${customer.email}`} className="truncate text-xs text-brand-text hover:underline">
                {customer.email}
              </a>
            </div>
          </div>
          {customer.phone && (
            <a href={`tel:${customer.phone}`} className="text-xs text-ink-muted hover:text-ink">
              {customer.phone}
            </a>
          )}
          {customer.external_reference && (
            <p className="border-t border-border-subtle pt-3 text-[0.6875rem] text-ink-subtle">
              Reference <span className="font-medium text-ink-muted">{customer.external_reference}</span>
            </p>
          )}
        </div>
      ) : (
        <p className="p-4 text-xs text-ink-muted">Customer details aren’t available for this role.</p>
      )}
    </Card>
  )
}

function AttachmentsPanel({
  attachments,
  loading,
  error,
  onRetry,
  onDownload,
  canUpload,
  uploading,
  onUpload,
}: {
  attachments?: Attachment[] | undefined
  loading: boolean
  error: unknown
  onRetry: () => void
  onDownload: (attachment: Attachment) => void
  canUpload: boolean
  uploading: boolean
  onUpload: (file: File) => Promise<void>
}) {
  const fileInput = useRef<HTMLInputElement>(null)

  return (
    <Card variant="bezel" className="overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border-subtle px-4 py-3.5">
        <Paperclip aria-hidden="true" className="size-4 text-ink-subtle" />
        <h2 className="text-sm font-semibold text-ink">Files</h2>
        {attachments && <Badge variant="neutral" size="sm" className={canUpload ? 'ml-auto' : undefined}>{attachments.length}</Badge>}
        {canUpload && (
          <>
            <input
              ref={fileInput}
              type="file"
              accept=".png,.jpg,.jpeg,.gif,.pdf,.txt,.log,.csv,.md,.markdown"
              className="sr-only"
              aria-label="Choose an attachment"
              onChange={(event) => {
                const input = event.currentTarget
                const file = input.files?.[0]
                if (file) void onUpload(file).then(() => { input.value = '' }).catch(() => undefined)
              }}
            />
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto px-2"
              disabled={uploading}
              loading={uploading}
              aria-label="Attach a file"
              leadingIcon={<UploadSimple aria-hidden="true" />}
              onClick={() => fileInput.current?.click()}
            >
              Attach
            </Button>
          </>
        )}
      </div>
      {loading ? (
        <div className="flex flex-col gap-3 p-4" aria-label="Loading attachments">
          <div className="h-9 animate-pulse rounded-lg bg-surface-sunken" />
          <div className="h-9 animate-pulse rounded-lg bg-surface-sunken" />
        </div>
      ) : error ? (
        <ErrorState
          title="Files couldn’t load"
          description="The ticket is available, but its attachments could not be retrieved."
          onRetry={onRetry}
          className="px-4 py-8"
        />
      ) : attachments?.length ? (
        <ul className="divide-y divide-border-subtle">
          {attachments.map((attachment) => (
            <li key={attachment.id} className="flex items-center gap-3 px-4 py-3">
              <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-surface-sunken text-ink-muted">
                <Paperclip aria-hidden="true" className="size-4" />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-medium text-ink" title={attachment.filename}>{attachment.filename}</p>
                <p className="mt-0.5 text-[0.625rem] text-ink-subtle">{formatBytes(attachment.size_bytes)}</p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Download ${attachment.filename}`}
                onClick={() => onDownload(attachment)}
                className="shrink-0 px-2"
                leadingIcon={<DownloadSimple className="size-4" />}
              >
                <span className="sr-only">Download</span>
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="px-4 py-5 text-xs text-ink-muted">No files attached to this ticket.</p>
      )}
    </Card>
  )
}

function TicketLoadingShell() {
  return (
    <div className="flex flex-col gap-5" aria-label="Loading ticket">
      <div className="flex flex-col gap-3">
        <div className="h-3 w-32 animate-pulse rounded bg-surface-sunken" />
        <div className="h-7 w-3/4 animate-pulse rounded bg-surface-sunken" />
        <div className="h-4 w-1/2 animate-pulse rounded bg-surface-sunken" />
      </div>
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_21rem]">
        <div className="h-[34rem] animate-pulse rounded-2xl bg-surface-sunken" />
        <div className="flex flex-col gap-4">
          <div className="h-36 animate-pulse rounded-2xl bg-surface-sunken" />
          <div className="h-44 animate-pulse rounded-2xl bg-surface-sunken" />
        </div>
      </div>
    </div>
  )
}

export function TicketDetailPage() {
  const { ticketId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const user = useSessionStore((state) => state.user)
  const [confirmation, setConfirmation] = useState<'close' | 'reopen' | null>(null)
  const [assignmentDraft, setAssignmentDraft] = useState<{ ticketId: string; value: string } | null>(null)

  const ticketQuery = useQuery({
    queryKey: ticketKeys.detail(ticketId),
    queryFn: async () => (await apiClient.get<Ticket>(`/tickets/${ticketId}`)).data,
    enabled: Boolean(ticketId),
  })
  const ticket = ticketQuery.data
  const assignmentChanged = assignmentDraft?.ticketId === ticketId &&
    assignmentDraft.value !== (ticket?.assigned_agent_id ?? '')
  const role = user?.role
  const canReadCustomer = can(role, PERMISSIONS.CUSTOMER_LIST)
  const canAssign = can(role, PERMISSIONS.TICKET_ASSIGN)
  const canAnalyze = can(role, PERMISSIONS.AI_REQUEST_ANALYSIS)
  const canSuggest = can(role, PERMISSIONS.AI_REQUEST_SUGGESTION) && can(role, PERMISSIONS.MESSAGE_POST_REPLY)
  const canUpload = can(role, PERMISSIONS.ATTACHMENT_UPLOAD)
  const canViewAudit = can(role, PERMISSIONS.AUDIT_VIEW)

  const customerQuery = useQuery({
    queryKey: customerKeys.detail(ticket?.customer_id ?? ''),
    queryFn: async () => (await apiClient.get<Customer>(`/customers/${ticket?.customer_id}`)).data,
    enabled: Boolean(ticket?.customer_id && canReadCustomer),
  })
  const eventsQuery = useQuery({
    queryKey: ticketKeys.events(ticketId),
    queryFn: async () => (await apiClient.get<TicketEvent[]>(`/tickets/${ticketId}/events`)).data,
    enabled: Boolean(ticketId && ticket),
  })
  const messagesQuery = useQuery({
    queryKey: ticketKeys.messages(ticketId),
    queryFn: async () =>
      (await apiClient.get<Message[]>(`/tickets/${ticketId}/messages`, { params: { limit: 200, offset: 0 } })).data,
    enabled: Boolean(ticketId && ticket),
  })
  const attachmentsQuery = useQuery({
    queryKey: ticketKeys.attachments(ticketId),
    queryFn: async () =>
      (await apiClient.get<Attachment[]>(`/tickets/${ticketId}/attachments`, { params: { limit: 100, offset: 0 } })).data,
    enabled: Boolean(ticketId && ticket),
  })
  const teamQuery = useQuery({
    queryKey: teamKeys.list({ limit: 100, offset: 0 }),
    queryFn: async () => (await apiClient.get<User[]>('/users', { params: { limit: 100, offset: 0 } })).data,
    enabled: canAssign,
  })

  const ticketMutation = useMutation<Ticket, unknown, TicketAction>({
    mutationFn: async (action) => {
      let response: { data: Ticket }
      if (action.kind === 'status') {
        response = await apiClient.post<Ticket>(`/tickets/${ticketId}/status`, { status: action.status })
      } else if (action.kind === 'priority') {
        response = await apiClient.post<Ticket>(`/tickets/${ticketId}/priority`, { priority: action.priority })
      } else if (action.kind === 'assignment') {
        response = await apiClient.post<Ticket>(`/tickets/${ticketId}/assign`, { assigned_agent_id: action.assigned_agent_id })
      } else if (action.kind === 'close') {
        response = await apiClient.post<Ticket>(`/tickets/${ticketId}/close`)
      } else {
        response = await apiClient.post<Ticket>(`/tickets/${ticketId}/reopen`)
      }
      return response.data
    },
    onSuccess: (updated, action) => {
      queryClient.setQueryData(ticketKeys.detail(ticketId), updated)
      void queryClient.invalidateQueries({ queryKey: ticketKeys.events(ticketId) })
      void queryClient.invalidateQueries({ queryKey: ticketKeys.lists() })
      setAssignmentDraft(null)
      setConfirmation(null)
      const successMessage =
        action.kind === 'status'
          ? `Ticket moved to ${TICKET_STATUS_LABELS[action.status].toLowerCase()}.`
          : action.kind === 'priority'
            ? `Priority changed to ${ticketPriorityLabel(action.priority).toLowerCase()}.`
            : action.kind === 'assignment'
              ? action.assigned_agent_id ? 'Ticket assignment updated.' : 'Ticket returned to the unassigned queue.'
              : action.kind === 'close'
                ? 'Ticket closed.'
                : 'Ticket reopened.'
      toast.success('Ticket updated', successMessage)
    },
    onError: (error) => {
      toast.error('Ticket update failed', toApiError(error).message)
    },
  })

  const messageMutation = useMutation<Message, unknown, { body: string; internal: boolean }>({
    mutationFn: async ({ body, internal }) => {
      const path = internal ? `/tickets/${ticketId}/notes` : `/tickets/${ticketId}/messages`
      return (await apiClient.post<Message>(path, { body })).data
    },
    onSuccess: (message) => {
      queryClient.setQueryData<Message[]>(ticketKeys.messages(ticketId), (current) =>
        current ? [...current, message] : [message],
      )
      void queryClient.invalidateQueries({ queryKey: ticketKeys.detail(ticketId) })
      void queryClient.invalidateQueries({ queryKey: ticketKeys.lists() })
      toast.success(message.is_internal ? 'Internal note added' : 'Reply sent')
    },
    onError: (error) => {
      toast.error('Message could not be sent', toApiError(error).message)
    },
  })

  const uploadMutation = useMutation<Attachment, unknown, File>({
    mutationFn: async (file) => {
      const form = new FormData()
      form.append('file', file)
      return (await apiClient.post<Attachment>(`/tickets/${ticketId}/attachments`, form)).data
    },
    onSuccess: (attachment) => {
      queryClient.setQueryData<Attachment[]>(ticketKeys.attachments(ticketId), (current) =>
        current ? [...current, attachment] : [attachment],
      )
      void queryClient.invalidateQueries({ queryKey: ticketKeys.events(ticketId) })
      void queryClient.invalidateQueries({ queryKey: ticketKeys.lists() })
      toast.success('File attached', `${attachment.filename} is now available on this ticket.`)
    },
    onError: (error) => toast.error('Upload failed', toApiError(error).message),
  })

  const acceptDraftMutation = useMutation<Message, unknown, { draft: Message; body: string }>({
    mutationFn: async ({ draft, body }) =>
      (await apiClient.post<Message>(`/tickets/${ticketId}/ai/drafts/${draft.id}/accept`, { body })).data,
    onSuccess: (message) => {
      queryClient.setQueryData<Message[]>(ticketKeys.messages(ticketId), (current) =>
        current ? [...current, message] : [message],
      )
      void queryClient.invalidateQueries({ queryKey: ticketKeys.events(ticketId) })
      void queryClient.invalidateQueries({ queryKey: ticketKeys.lists() })
      toast.success('Reply sent', 'Your reviewed reply was sent to the customer.')
    },
    onError: (error) => toast.error('Draft could not be sent', toApiError(error).message),
  })

  const downloadAttachment = async (attachment: Attachment) => {
    try {
      const { data } = await apiClient.get<Blob>(`/attachments/${attachment.id}`, { responseType: 'blob' })
      const url = URL.createObjectURL(data)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = attachment.filename
      anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1_000)
    } catch (error) {
      toast.error('Download failed', toApiError(error).message)
    }
  }

  if (!user) return null

  const backTo = isPortalRole(user.role) ? '/portal' : '/tickets'
  const pageLabel = isPortalRole(user.role) ? 'Your requests' : 'Ticket queue'
  const ticketError = ticketQuery.error ? toApiError(ticketQuery.error) : null

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      {ticketQuery.isPending ? (
        <TicketLoadingShell />
      ) : !ticket ? (
        <div className="mx-auto max-w-xl py-8">
          <ErrorState
            title={ticketError?.status === 404 ? 'Ticket not found' : 'This ticket couldn’t load'}
            description={ticketError?.status === 404
              ? 'It may have moved, or your role may no longer have access to it.'
              : ticketError?.message ?? 'Try again in a moment.'}
            onRetry={() => void ticketQuery.refetch()}
          />
          <div className="mt-3 flex justify-center">
            <Button variant="secondary" size="sm" onClick={() => navigate(backTo)} leadingIcon={<ArrowLeft />}>{pageLabel}</Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-5">
          <Link
            to={backTo}
            className="inline-flex w-fit items-center gap-2 rounded-md text-xs font-medium text-ink-muted transition-colors duration-200 ease-out-expo hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            <ArrowLeft aria-hidden="true" className="size-3.5" />
            {pageLabel}
          </Link>

          <header className="flex flex-col gap-4 border-b border-border-subtle pb-5 lg:flex-row lg:items-end lg:justify-between">
            <div className="flex min-w-0 flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs font-medium tracking-wide text-ink-subtle">#{ticket.number}</span>
                <Badge variant={TICKET_STATUS_VARIANTS[ticket.status]} size="sm" dot>
                  {TICKET_STATUS_LABELS[ticket.status]}
                </Badge>
                <Badge variant={TICKET_PRIORITY_VARIANTS[ticket.priority]} size="sm" dot>
                  {ticketPriorityLabel(ticket.priority)} priority
                </Badge>
              </div>
              <h1 className="max-w-4xl text-2xl font-semibold tracking-[-0.035em] text-ink sm:text-3xl">
                {ticket.subject}
              </h1>
              <p className="text-xs text-ink-muted">
                Created {formatDate(ticket.created_at)} · Updated {formatDate(ticket.updated_at)}
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {canViewAudit && <Link to={auditHistoryUrl('ticket', ticket.id)} className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-border-subtle bg-surface px-3 text-xs font-medium text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink">Audit history <ArrowRight aria-hidden="true" className="size-3.5" /></Link>}
              {can(role, PERMISSIONS.TICKET_CHANGE_PRIORITY) && (
                <label className="sr-only" htmlFor="ticket-detail-priority">Change priority</label>
              )}
              {can(role, PERMISSIONS.TICKET_CHANGE_PRIORITY) ? (
                <Select
                  id="ticket-detail-priority"
                  aria-label="Change ticket priority"
                  value={ticket.priority}
                  disabled={ticketMutation.isPending}
                  onChange={(event) => ticketMutation.mutate({ kind: 'priority', priority: event.target.value as TicketPriority })}
                  className="h-9 min-w-32 bg-surface text-xs"
                >
                  <option value="urgent">Urgent priority</option>
                  <option value="high">High priority</option>
                  <option value="medium">Medium priority</option>
                  <option value="low">Low priority</option>
                </Select>
              ) : null}

              {(can(role, PERMISSIONS.TICKET_CHANGE_STATUS) ? STATUS_ACTIONS[ticket.status] ?? [] : [])
                .map((action) => (
                  <Button
                    key={action.status}
                    variant={action.variant ?? 'secondary'}
                    size="sm"
                    disabled={ticketMutation.isPending}
                    onClick={() => ticketMutation.mutate({ kind: 'status', status: action.status })}
                    trailingIcon={<ArrowRight aria-hidden="true" />}
                  >
                    {action.label}
                  </Button>
                ))}

              {ticket.status === 'resolved' && can(role, PERMISSIONS.TICKET_CLOSE) && (
                <Button
                  size="sm"
                  disabled={ticketMutation.isPending}
                  onClick={() => setConfirmation('close')}
                  leadingIcon={<CheckCircle weight="bold" />}
                >
                  Close ticket
                </Button>
              )}
              {ticket.status === 'closed' && can(role, PERMISSIONS.TICKET_REOPEN) && (
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={ticketMutation.isPending}
                  onClick={() => setConfirmation('reopen')}
                  leadingIcon={<ArrowRight />}
                >
                  Reopen ticket
                </Button>
              )}
            </div>
          </header>

          <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_21rem]">
            <div className="flex min-w-0 flex-col gap-5">
              <TicketConversation
                ticket={ticket}
                customer={customerQuery.data}
                messages={messagesQuery.data}
                messagesLoading={messagesQuery.isPending}
                messagesError={messagesQuery.error}
                onRetryMessages={() => { void messagesQuery.refetch() }}
                currentUserId={user.id}
                canAcceptDraft={canSuggest && can(role, PERMISSIONS.MESSAGE_POST_REPLY) && ticket.status !== 'closed'}
                onAcceptDraft={async (draft, body) => { await acceptDraftMutation.mutateAsync({ draft, body }) }}
                canReply={can(role, PERMISSIONS.MESSAGE_POST_REPLY)}
                canPostInternal={can(role, PERMISSIONS.MESSAGE_POST_INTERNAL)}
                sending={messageMutation.isPending}
                onSend={async (body, internal) => { await messageMutation.mutateAsync({ body, internal }) }}
              />

              {canAssign && (
                <Card variant="bezel" className="overflow-hidden">
                  <div className="flex items-center justify-between gap-4 border-b border-border-subtle px-4 py-3.5">
                    <div className="flex items-center gap-2">
                      <UserCircle aria-hidden="true" className="size-4 text-ink-subtle" />
                      <h2 className="text-sm font-semibold text-ink">Assignment</h2>
                    </div>
                    {ticket.assigned_agent_id === user.id ? (
                      <Badge variant="brand" size="sm">Assigned to you</Badge>
                    ) : ticket.assigned_agent_id ? (
                      <Badge variant="neutral" size="sm">Assigned</Badge>
                    ) : (
                      <Badge variant="warning" size="sm">Unassigned</Badge>
                    )}
                  </div>
                  <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-end">
                    <div className="min-w-0 flex-1">
                      <label className="mb-1.5 block text-xs font-medium text-ink-muted" htmlFor="ticket-assignee">
                        Assign to an agent
                      </label>
                      {teamQuery.isError ? (
                        <p className="text-xs text-danger-text">Agent list unavailable. Try refreshing the page.</p>
                      ) : (
                        <Select
                          id="ticket-assignee"
                          value={assignmentDraft?.ticketId === ticketId ? assignmentDraft.value : ticket.assigned_agent_id ?? ''}
                          disabled={teamQuery.isPending || ticketMutation.isPending}
                          onChange={(event) => setAssignmentDraft({ ticketId, value: event.target.value })}
                          aria-label="Assign ticket to an agent"
                          className="bg-surface"
                        >
                          <option value="">Unassigned</option>
                          {ticket.assigned_agent_id && !teamQuery.data?.some((member) => member.id === ticket.assigned_agent_id) && (
                            <option value={ticket.assigned_agent_id}>Current assignee</option>
                          )}
                          {teamQuery.data?.filter((member) => member.role === 'agent' && member.is_active).map((agent) => (
                            <option key={agent.id} value={agent.id}>{agent.name}</option>
                          ))}
                        </Select>
                      )}
                    </div>
                    <Button
                      variant="secondary"
                      size="md"
                      disabled={
                        teamQuery.isPending ||
                        ticketMutation.isPending ||
                        !assignmentChanged
                      }
                      onClick={() => {
                        if (assignmentDraft?.ticketId === ticketId) {
                          ticketMutation.mutate({
                            kind: 'assignment',
                            assigned_agent_id: assignmentDraft.value || null,
                          })
                        }
                      }}
                    >
                      Save assignment
                    </Button>
                  </div>
                </Card>
              )}
            </div>

            <aside className="flex flex-col gap-4">
              <CustomerPanel customer={customerQuery.data} loading={customerQuery.isPending && canReadCustomer} />
              <TicketAssistant ticketId={ticketId} canAnalyze={canAnalyze} canSuggest={canSuggest && ticket.status !== 'closed'} />
              <TicketSlaPanel ticket={ticket} />
              <AttachmentsPanel
                attachments={attachmentsQuery.data}
                loading={attachmentsQuery.isPending}
                error={attachmentsQuery.error}
                onRetry={() => { void attachmentsQuery.refetch() }}
                onDownload={(attachment) => { void downloadAttachment(attachment) }}
                canUpload={canUpload}
                uploading={uploadMutation.isPending}
                onUpload={async (file) => { await uploadMutation.mutateAsync(file) }}
              />
              <TicketActivity
                events={eventsQuery.data}
                loading={eventsQuery.isPending}
                error={eventsQuery.error}
                currentUserId={user.id}
                onRetry={() => { void eventsQuery.refetch() }}
              />
            </aside>
          </div>
        </div>
      )}

      <Dialog
        open={confirmation !== null}
        onClose={() => setConfirmation(null)}
        title={confirmation === 'close' ? `Close ticket #${ticket?.number}?` : `Reopen ticket #${ticket?.number}?`}
        description={confirmation === 'close'
          ? 'The ticket will move to Closed. You can reopen it later if the customer needs more help.'
          : 'The ticket will return to Open and its current assignment will be cleared.'}
        footer={(
          <>
            <Button variant="secondary" size="sm" onClick={() => setConfirmation(null)}>Cancel</Button>
            <Button
              variant={confirmation === 'close' ? 'primary' : 'secondary'}
              size="sm"
              loading={ticketMutation.isPending}
              onClick={() => {
                if (confirmation) ticketMutation.mutate({ kind: confirmation })
              }}
            >
              {confirmation === 'close' ? 'Close ticket' : 'Reopen ticket'}
            </Button>
          </>
        )}
      >
        <div className={cn(
          'rounded-xl border px-4 py-3 text-sm leading-relaxed',
          confirmation === 'close' ? 'border-border-subtle bg-surface-sunken text-ink-muted' : 'border-info-border bg-info-subtle text-info-text',
        )}>
          {confirmation === 'close'
            ? 'Closing confirms that the resolved work is complete.'
            : 'Reopening returns this request to the active support queue.'}
        </div>
      </Dialog>
    </AppShell>
  )
}

