/** Query keys for staff records, shared by team and ticket assignment screens. */
export interface TeamListFilters {
  limit?: number
  offset?: number
}

export const teamKeys = {
  all: ['team'] as const,
  lists: () => [...teamKeys.all, 'list'] as const,
  list: (filters: TeamListFilters = {}) => [...teamKeys.lists(), filters] as const,
}
