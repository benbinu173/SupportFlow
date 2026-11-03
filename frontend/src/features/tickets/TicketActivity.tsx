import { ClockCounterClockwise } from '@phosphor-icons/react'

import { Badge } from '@/components/ui/Badge'
import { Card } from '@/components/ui/Card'
import { ErrorState } from '@/components/ui/States'
import { Skeleton } from '@/components/ui/Skeleton'
import { TICKET_STATUS_LABELS, ticketPriorityLabel } from '@/features/tickets/presentation'
import type { components } from '@/types/api'

type TicketEvent = components['schemas']['TicketEventRead']
type TicketEventType = components['schemas']['TicketEventType']
type TicketStatus = components['schemas']['TicketStatus']
type TicketPriority = components['schemas']['TicketPriority']

const EVENT_LABELS: Record<TicketEventType, string> = {
  created: 'Ticket created',
  assigned: 'Ticket assigned',
  unassigned: 'Ticket unassigned',
  status_changed: 'Status changed',
  priority_changed: 'Priority changed',
  message_added: 'Reply added',
  internal_note_added: 'Internal note added',
  attachment_added: 'Attachment added',
  ai_analysis_completed: 'AI analysis completed',
  sla_warning: 'SLA warning',
  sla_breached: 'SLA breached',
  reopened: 'Ticket reopened',
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

function statusLabel(value: string | null): string | null {
  if (!value || !Object.prototype.hasOwnProperty.call(TICKET_STATUS_LABELS, value)) return value
  return TICKET_STATUS_LABELS[value as TicketStatus]
}

function priorityLabel(value: string | null): string | null {
  if (!value || !['low', 'medium', 'high', 'urgent'].includes(value)) return value
  return ticketPriorityLabel(value as TicketPriority)
}

function eventDescription(event: TicketEvent): string | null {
  if (event.event_type === 'status_changed') {
    const from = statusLabel(event.from_value)
    const to = statusLabel(event.to_value)
    return from && to ? `${from} → ${to}` : to ? `Moved to ${to}` : null
  }
  if (event.event_type === 'priority_changed') {
    const from = priorityLabel(event.from_value)
    const to = priorityLabel(event.to_value)
    return from && to ? `${from} → ${to}` : to ? `Changed to ${to}` : null
  }
  if (event.event_type === 'message_added') return 'A customer-facing reply was added.'
  if (event.event_type === 'internal_note_added') return 'A private note was added for the support team.'
  if (event.event_type === 'attachment_added') return 'A file was added to the conversation.'
  if (event.event_type === 'ai_analysis_completed') return 'Ticket classification and sentiment were updated.'
  if (event.event_type === 'sla_warning') return 'The response or resolution target is approaching.'
  if (event.event_type === 'sla_breached') return 'An SLA target was missed.'
  if (event.event_type === 'assigned') return 'An owner was set for this ticket.'
  if (event.event_type === 'unassigned') return 'The ticket returned to the unassigned queue.'
  if (event.event_type === 'reopened') return 'The ticket was returned to the active queue.'
  return null
}

function eventActor(event: TicketEvent, currentUserId: string): string {
  if (event.actor_user_id === currentUserId) return 'You'
  if (event.actor_user_id) return 'Team member'
  return 'Automation'
}

export function TicketActivity({
  events,
  loading,
  error,
  currentUserId,
  onRetry,
}: {
  events?: TicketEvent[]
  loading: boolean
  error: unknown
  currentUserId: string
  onRetry: () => void
}) {
  return (
    <Card variant="bezel" className="overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border-subtle px-4 py-3.5">
        <span className="grid size-7 place-items-center rounded-lg bg-surface-sunken text-ink-muted">
          <ClockCounterClockwise aria-hidden="true" className="size-4" />
        </span>
        <h2 className="text-sm font-semibold text-ink">Activity</h2>
        {events && <Badge variant="neutral" size="sm" className="ml-auto">{events.length}</Badge>}
      </div>

      {loading ? (
        <div className="flex flex-col gap-5 px-4 py-5" aria-label="Loading ticket activity">
          {Array.from({ length: 4 }, (_, index) => (
            <div key={index} className="flex gap-3">
              <Skeleton className="mt-1 size-2 shrink-0 rounded-full" />
              <div className="flex flex-1 flex-col gap-2">
                <Skeleton className="h-3 w-3/5" />
                <Skeleton className="h-3 w-4/5" />
              </div>
            </div>
          ))}
        </div>
      ) : error ? (
        <ErrorState
          title="Activity couldn’t load"
          description="The ticket is available, but its history could not be retrieved."
          onRetry={onRetry}
          className="px-4 py-8"
        />
      ) : events?.length ? (
        <ol className="max-h-[32rem] overflow-y-auto px-4 py-4">
          {events.map((event, index) => (
            <li key={event.id} className="relative flex gap-3 pb-5 last:pb-0">
              {index < events.length - 1 && (
                <span aria-hidden="true" className="absolute top-3 left-[0.1875rem] h-full w-px bg-border-subtle" />
              )}
              <span
                aria-hidden="true"
                className={`relative mt-1 size-1.5 shrink-0 rounded-full ${event.event_type.startsWith('sla_') ? 'bg-warning' : 'bg-brand'}`}
              />
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
                  <p className="text-xs font-semibold text-ink">{EVENT_LABELS[event.event_type]}</p>
                  <time dateTime={event.created_at} className="text-[0.625rem] text-ink-subtle">
                    {formatTime(event.created_at)}
                  </time>
                </div>
                {eventDescription(event) && (
                  <p className="text-xs leading-relaxed text-ink-muted">{eventDescription(event)}</p>
                )}
                <p className="text-[0.625rem] text-ink-subtle">{eventActor(event, currentUserId)}</p>
                {event.event_type === 'ai_analysis_completed' && (
                  <Badge variant="ai" size="sm" className="mt-1 w-fit">AI assisted</Badge>
                )}
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="px-4 py-6 text-xs text-ink-muted">No activity has been recorded yet.</p>
      )}
    </Card>
  )
}

