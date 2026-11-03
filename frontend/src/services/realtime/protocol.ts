import type { components } from '@/types/api'

/** Names and envelope fields mirror backend/app/websocket/events.py. */
export const REALTIME_EVENT_TYPES = [
  'ticket.created',
  'ticket.assigned',
  'ticket.unassigned',
  'ticket.status_changed',
  'ticket.priority_changed',
  'ticket.message_added',
  'ticket.note_added',
  'ticket.attachment_added',
  'ticket.reopened',
  'ticket.ai_analysis_completed',
  'notification.created',
] as const

export type RealtimeEventType = (typeof REALTIME_EVENT_TYPES)[number]
export type NotificationType = components['schemas']['NotificationType']

export interface TicketEventEnvelope {
  type: Exclude<RealtimeEventType, 'notification.created'>
  organization_id: string
  ticket_id: string
  ticket_number: number
  from_value: string | null
  to_value: string | null
  assigned_agent_id: string | null
  customer_id: string | null
  internal: boolean
}

export interface NotificationEnvelope {
  type: 'notification.created'
  organization_id: string
  user_id: string
  notification_id: string
  notification_type: NotificationType
  title: string
  ticket_id: string | null
}

export type RealtimeEnvelope = TicketEventEnvelope | NotificationEnvelope

export function isRealtimeEnvelope(value: unknown): value is RealtimeEnvelope {
  if (!value || typeof value !== 'object') return false
  const envelope = value as Record<string, unknown>
  if (!REALTIME_EVENT_TYPES.some((type) => type === envelope.type)) return false

  if (envelope.type === 'notification.created') {
    return (
      typeof envelope.organization_id === 'string' &&
      typeof envelope.user_id === 'string' &&
      typeof envelope.notification_id === 'string' &&
      typeof envelope.title === 'string'
    )
  }

  return (
    typeof envelope.organization_id === 'string' &&
    typeof envelope.ticket_id === 'string' &&
    typeof envelope.ticket_number === 'number'
  )
}
