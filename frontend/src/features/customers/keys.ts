import type { components } from '@/types/api'

export interface CustomerListFilters {
  q?: string | null
  created_after?: string | null
  created_before?: string | null
  sort?: components['schemas']['CustomerSortKey']
  order?: components['schemas']['SortOrder']
  limit?: number
  offset?: number
}

/** Query keys for customer records linked to tickets and customer screens. */
export const customerKeys = {
  all: ['customers'] as const,
  lists: () => [...customerKeys.all, 'list'] as const,
  list: (filters: CustomerListFilters = {}) => [...customerKeys.lists(), filters] as const,
  details: () => [...customerKeys.all, 'detail'] as const,
  detail: (customerId: string) => [...customerKeys.details(), customerId] as const,
}
