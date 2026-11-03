import type { QueryClient } from '@tanstack/react-query'

import { notificationKeys } from '@/features/notifications/keys'
import { analyticsKeys } from '@/features/analytics/keys'
import { ticketKeys } from '@/features/tickets/keys'

import type { RealtimeEnvelope, RealtimeEventType } from './protocol'

/**
 * Client counterpart to backend/app/websocket/events.py::REALTIME_FOR_EVENT.
 * Keep each event's effects explicit; consumers and this map share key factories.
 */
export const REALTIME_INVALIDATIONS: Record<RealtimeEventType, readonly ('ticket-list' | 'ticket-detail' | 'ticket-events' | 'ticket-messages' | 'ticket-attachments' | 'ticket-ai' | 'notifications')[]> = {
  'ticket.created': ['ticket-list'],
  'ticket.assigned': ['ticket-list', 'ticket-detail', 'ticket-events'],
  'ticket.unassigned': ['ticket-list', 'ticket-detail', 'ticket-events'],
  'ticket.status_changed': ['ticket-list', 'ticket-detail', 'ticket-events'],
  'ticket.priority_changed': ['ticket-list', 'ticket-detail', 'ticket-events'],
  'ticket.message_added': ['ticket-list', 'ticket-detail', 'ticket-events', 'ticket-messages'],
  'ticket.note_added': ['ticket-list', 'ticket-detail', 'ticket-events', 'ticket-messages'],
  'ticket.attachment_added': ['ticket-list', 'ticket-detail', 'ticket-events', 'ticket-messages', 'ticket-attachments'],
  'ticket.reopened': ['ticket-list', 'ticket-detail', 'ticket-events'],
  'ticket.ai_analysis_completed': ['ticket-list', 'ticket-detail', 'ticket-events', 'ticket-messages', 'ticket-ai'],
  'notification.created': ['notifications'],
}

export function invalidateForEnvelope(queryClient: QueryClient, envelope: RealtimeEnvelope): void {
  const targets = REALTIME_INVALIDATIONS[envelope.type]

  // Ticket changes alter the aggregates shown on both the dashboard and the
  // analytics workspace. Keep those views current across browser sessions.
  if (envelope.type.startsWith('ticket.')) {
    void queryClient.invalidateQueries({ queryKey: analyticsKeys.all })
  }

  for (const target of targets) {
    if (target === 'ticket-list') {
      void queryClient.invalidateQueries({ queryKey: ticketKeys.lists() })
    } else if (target === 'notifications') {
      void queryClient.invalidateQueries({ queryKey: notificationKeys.all })
    } else if (envelope.type !== 'notification.created') {
      if (target === 'ticket-detail') {
        void queryClient.invalidateQueries({ queryKey: ticketKeys.detail(envelope.ticket_id) })
      } else if (target === 'ticket-events') {
        void queryClient.invalidateQueries({ queryKey: ticketKeys.events(envelope.ticket_id) })
      } else if (target === 'ticket-messages') {
        void queryClient.invalidateQueries({ queryKey: ticketKeys.messages(envelope.ticket_id) })
      } else if (target === 'ticket-attachments') {
        void queryClient.invalidateQueries({ queryKey: ticketKeys.attachments(envelope.ticket_id) })
      } else if (target === 'ticket-ai') {
        void queryClient.invalidateQueries({ queryKey: ticketKeys.aiAnalyses(envelope.ticket_id) })
      }
    }
  }
}
