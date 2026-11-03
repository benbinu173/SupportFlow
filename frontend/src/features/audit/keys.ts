export interface AuditFilters {
  action?: string
  actor_user_id?: string
  target_type?: string
  created_after?: string
  created_before?: string
  limit: number
  offset: number
}

export const auditKeys = {
  all: ['audit'] as const,
  list: (filters: AuditFilters) => [...auditKeys.all, 'list', filters] as const,
}
