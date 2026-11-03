import type { components } from '@/types/api'

/** Query keys for ticket data. Keep these as the single address book for F4+. */
export interface TicketListFilters {
  status?: components['schemas']['TicketStatus'] | null
  priority?: components['schemas']['TicketPriority'] | null
  assigned_agent_id?: string | null
  unassigned?: boolean
  customer_id?: string | null
  q?: string | null
  created_after?: string | null
  created_before?: string | null
  sort?: components['schemas']['TicketSortKey']
  order?: components['schemas']['SortOrder']
  limit?: number
  offset?: number
}

export const ticketKeys = {
  all: ['tickets'] as const,
  lists: () => [...ticketKeys.all, 'list'] as const,
  list: (filters: TicketListFilters = {}) => [...ticketKeys.lists(), filters] as const,
  details: () => [...ticketKeys.all, 'detail'] as const,
  detail: (ticketId: string) => [...ticketKeys.details(), ticketId] as const,
  events: (ticketId: string) => [...ticketKeys.detail(ticketId), 'events'] as const,
  messages: (ticketId: string) => [...ticketKeys.detail(ticketId), 'messages'] as const,
}
