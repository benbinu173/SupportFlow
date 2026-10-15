import type { UserRole } from '@/features/auth/permissions'

/**
 * The signed-in user, as the client holds them.
 *
 * Field names deliberately match `UserRead` in
 * `backend/app/schemas/user.py` rather than being camel-cased. Server data
 * keeps the server's spelling throughout this codebase, so there is no
 * translation layer to drift and — once the OpenAPI types land — this interface
 * has nothing to reconcile with. Only *app* state (a theme preference, whether
 * a drawer is open) is camelCase.
 *
 * `organization_id` is absent because the API does not return it: the tenant is
 * implied by the caller's own token, and echoing it back would suggest it is
 * theirs to set. Anything the client needs to know about the organization comes
 * from an organization-scoped endpoint or not at all.
 */
export interface SessionUser {
  id: string
  name: string
  email: string
  role: UserRole
  is_active: boolean
  /** Non-null for a portal account, naming the customer it acts as. */
  customer_id: string | null
  created_at?: string
  last_login_at?: string | null
}
