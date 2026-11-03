import type { BadgeVariant } from '@/components/ui/Badge'
import type { components } from '@/types/api'

export type TicketStatus = components['schemas']['TicketStatus']
export type TicketPriority = components['schemas']['TicketPriority']

export const TICKET_STATUS_LABELS: Record<TicketStatus, string> = {
  open: 'Open',
  assigned: 'Assigned',
  in_progress: 'In progress',
  waiting_for_customer: 'Waiting for customer',
  resolved: 'Resolved',
  closed: 'Closed',
}

export const TICKET_STATUS_VARIANTS: Record<TicketStatus, BadgeVariant> = {
  open: 'brand',
  assigned: 'info',
  in_progress: 'warning',
  waiting_for_customer: 'neutral',
  resolved: 'success',
  closed: 'neutral',
}

export const TICKET_PRIORITY_VARIANTS: Record<TicketPriority, BadgeVariant> = {
  low: 'neutral',
  medium: 'info',
  high: 'warning',
  urgent: 'danger',
}

export function ticketPriorityLabel(priority: TicketPriority): string {
  return priority[0].toUpperCase() + priority.slice(1)
}

