import type { components } from '@/types/api'

/**
 * The signed-in user, as the client holds them.
 *
 * This is the API's own `UserRead`, read straight out of the generated schema
 * rather than restated. It was a hand-written mirror until the OpenAPI types
 * landed, and the mirror had already drifted: it marked `created_at` and
 * `last_login_at` optional, while `UserRead` — quite correctly — requires both,
 * with `last_login_at` nullable rather than absent. Nobody noticed, because
 * nothing in F2 reads either field. That is exactly how a mirror fails: it rots
 * silently until the day something does read the field.
 *
 * Field names stay as the server spells them rather than being camel-cased.
 * Server data keeps the server's spelling throughout this codebase, so there is
 * no translation layer to drift. Only *app* state (a theme preference, whether
 * a drawer is open) is camelCase.
 *
 * `organization_id` is absent because the API does not return it: the tenant is
 * implied by the caller's own token, and echoing it back would suggest it is
 * theirs to set. Anything the client needs to know about the organization comes
 * from an organization-scoped endpoint or not at all.
 */
export type SessionUser = components['schemas']['UserRead']
