export interface AuditFilters {
  action?: string
  actor_user_id?: string
  target_type?: string
  target_id?: string
  created_after?: string
  created_before?: string
  limit: number
  offset: number
}

export const auditKeys = {
  all: ['audit'] as const,
  list: (filters: AuditFilters) => [...auditKeys.all, 'list', filters] as const,
}

export function auditHistoryUrl(targetType: string, targetId: string): string {
  const params = new URLSearchParams({ target_type: targetType, target_id: targetId })
  return `/audit?${params.toString()}`
}
