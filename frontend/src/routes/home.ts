import type { UserRole } from '@/features/auth/permissions'

/** Shared by auth and route guards without importing the sidebar's icons. */
export const HOME_BY_ROLE: Record<UserRole, string> = {
  admin: '/dashboard',
  manager: '/dashboard',
  agent: '/dashboard',
  customer: '/portal',
}
