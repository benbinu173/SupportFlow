/**
 * The capability matrix, mirrored from `backend/app/core/permissions.py`.
 *
 * **This is a mirror, and it is not a security control.** That is not a
 * disclaimer bolted on for form's sake — it is the same statement
 * `docs/requirements.md` §3 makes, and `permissions.py` makes about its own
 * relationship to the test that checks it: every route re-evaluates the matrix
 * server-side, and a client that has been tampered with gains exactly nothing
 * by believing it holds a permission it does not. What this file buys is that a
 * customer is not shown an Assign button that would 403 — the difference
 * between an interface that respects a role and one that merely refuses them.
 *
 * Because it is a mirror, it is worth checking against the original by eye when
 * either changes. `ROLE_PERMISSIONS` is written out per role rather than
 * derived, for the same reason the Python table is: an accidental grant should
 * be visible in a diff.
 */

export const PERMISSIONS = {
  // Organization
  ORG_VIEW: 'org:view',
  ORG_UPDATE: 'org:update',

  // Users
  USER_LIST: 'user:list',
  USER_CREATE: 'user:create',
  USER_UPDATE_ROLE: 'user:update_role',
  USER_DEACTIVATE: 'user:deactivate',
  PROFILE_VIEW: 'profile:view',

  // Customers
  CUSTOMER_LIST: 'customer:list',
  CUSTOMER_CREATE: 'customer:create',
  CUSTOMER_UPDATE: 'customer:update',

  // Tickets
  TICKET_LIST: 'ticket:list',
  TICKET_CREATE: 'ticket:create',
  TICKET_VIEW: 'ticket:view',
  TICKET_ASSIGN: 'ticket:assign',
  TICKET_CHANGE_PRIORITY: 'ticket:change_priority',
  TICKET_CHANGE_STATUS: 'ticket:change_status',
  TICKET_CLOSE: 'ticket:close',
  TICKET_REOPEN: 'ticket:reopen',

  // Messages
  MESSAGE_READ_PUBLIC: 'message:read_public',
  MESSAGE_READ_INTERNAL: 'message:read_internal',
  MESSAGE_POST_REPLY: 'message:post_reply',
  MESSAGE_POST_INTERNAL: 'message:post_internal',

  // Attachments
  ATTACHMENT_UPLOAD: 'attachment:upload',
  ATTACHMENT_DOWNLOAD: 'attachment:download',

  // AI
  AI_REQUEST_ANALYSIS: 'ai:request_analysis',
  AI_REQUEST_SUGGESTION: 'ai:request_suggestion',
  AI_QUERY_KNOWLEDGE: 'ai:query_knowledge',
  AI_CONFIGURE: 'ai:configure',
  AI_VIEW_USAGE: 'ai:view_usage',

  // Knowledge base
  KB_LIST: 'kb:list',
  KB_UPLOAD: 'kb:upload',
  KB_DELETE: 'kb:delete',

  // SLA
  SLA_VIEW: 'sla:view',
  SLA_CONFIGURE: 'sla:configure',

  // Analytics
  ANALYTICS_ORG: 'analytics:org',
  ANALYTICS_OWN: 'analytics:own',

  // Audit
  AUDIT_VIEW: 'audit:view',

  // Notifications
  NOTIFICATION_LIST: 'notification:list',
} as const

export type Permission = (typeof PERMISSIONS)[keyof typeof PERMISSIONS]

export const USER_ROLES = ['admin', 'manager', 'agent', 'customer'] as const
export type UserRole = (typeof USER_ROLES)[number]

const ALL_PERMISSIONS = Object.values(PERMISSIONS)

/** Admin holds every capability, as in the Python table. */
const ADMIN_PERMISSIONS: readonly Permission[] = ALL_PERMISSIONS

const MANAGER_PERMISSIONS: readonly Permission[] = [
  PERMISSIONS.ORG_VIEW, // but not ORG_UPDATE
  PERMISSIONS.USER_LIST, // but not create / update role / deactivate
  PERMISSIONS.PROFILE_VIEW,
  PERMISSIONS.CUSTOMER_LIST,
  PERMISSIONS.CUSTOMER_CREATE,
  PERMISSIONS.CUSTOMER_UPDATE,
  PERMISSIONS.TICKET_LIST,
  PERMISSIONS.TICKET_CREATE,
  PERMISSIONS.TICKET_VIEW,
  PERMISSIONS.TICKET_ASSIGN,
  PERMISSIONS.TICKET_CHANGE_PRIORITY,
  PERMISSIONS.TICKET_CHANGE_STATUS,
  PERMISSIONS.TICKET_CLOSE,
  PERMISSIONS.TICKET_REOPEN,
  PERMISSIONS.MESSAGE_READ_PUBLIC,
  PERMISSIONS.MESSAGE_READ_INTERNAL,
  PERMISSIONS.MESSAGE_POST_REPLY,
  PERMISSIONS.MESSAGE_POST_INTERNAL,
  PERMISSIONS.ATTACHMENT_UPLOAD,
  PERMISSIONS.ATTACHMENT_DOWNLOAD,
  PERMISSIONS.AI_REQUEST_ANALYSIS,
  PERMISSIONS.AI_REQUEST_SUGGESTION,
  PERMISSIONS.AI_QUERY_KNOWLEDGE,
  PERMISSIONS.AI_VIEW_USAGE, // but not AI_CONFIGURE
  PERMISSIONS.KB_LIST, // but not upload / delete
  PERMISSIONS.SLA_VIEW, // but not SLA_CONFIGURE
  PERMISSIONS.ANALYTICS_ORG,
  PERMISSIONS.ANALYTICS_OWN,
  PERMISSIONS.NOTIFICATION_LIST,
]

const AGENT_PERMISSIONS: readonly Permission[] = [
  PERMISSIONS.PROFILE_VIEW,
  PERMISSIONS.CUSTOMER_LIST,
  PERMISSIONS.CUSTOMER_CREATE,
  PERMISSIONS.CUSTOMER_UPDATE,
  // Ticket and message capabilities are held but scoped to assigned work — see
  // TICKET_SCOPE_BY_ROLE. An agent cannot assign or reprioritise at all, and
  // cannot act on a ticket that is not theirs.
  PERMISSIONS.TICKET_LIST,
  PERMISSIONS.TICKET_CREATE,
  PERMISSIONS.TICKET_VIEW,
  PERMISSIONS.TICKET_CHANGE_STATUS,
  PERMISSIONS.TICKET_CLOSE,
  PERMISSIONS.TICKET_REOPEN,
  PERMISSIONS.MESSAGE_READ_PUBLIC,
  PERMISSIONS.MESSAGE_READ_INTERNAL,
  PERMISSIONS.MESSAGE_POST_REPLY,
  PERMISSIONS.MESSAGE_POST_INTERNAL,
  PERMISSIONS.ATTACHMENT_UPLOAD,
  PERMISSIONS.ATTACHMENT_DOWNLOAD,
  PERMISSIONS.AI_REQUEST_ANALYSIS,
  PERMISSIONS.AI_REQUEST_SUGGESTION,
  PERMISSIONS.AI_QUERY_KNOWLEDGE,
  PERMISSIONS.KB_LIST,
  PERMISSIONS.SLA_VIEW,
  PERMISSIONS.ANALYTICS_OWN, // own performance only
  PERMISSIONS.NOTIFICATION_LIST,
]

const CUSTOMER_PERMISSIONS: readonly Permission[] = [
  PERMISSIONS.PROFILE_VIEW,
  // Scoped to their own tickets. No internal notes, no customer directory, no
  // AI, no knowledge base, no analytics.
  PERMISSIONS.TICKET_LIST,
  PERMISSIONS.TICKET_CREATE,
  PERMISSIONS.TICKET_VIEW,
  PERMISSIONS.TICKET_CLOSE,
  PERMISSIONS.TICKET_REOPEN,
  PERMISSIONS.MESSAGE_READ_PUBLIC,
  PERMISSIONS.MESSAGE_POST_REPLY,
  PERMISSIONS.ATTACHMENT_UPLOAD,
  PERMISSIONS.ATTACHMENT_DOWNLOAD,
  PERMISSIONS.NOTIFICATION_LIST,
]

export const ROLE_PERMISSIONS: Record<UserRole, ReadonlySet<Permission>> = {
  admin: new Set(ADMIN_PERMISSIONS),
  manager: new Set(MANAGER_PERMISSIONS),
  agent: new Set(AGENT_PERMISSIONS),
  customer: new Set(CUSTOMER_PERMISSIONS),
}

/**
 * Rows within the tenant a role's queries may reach.
 *
 * The scope qualifiers (`all` / `assigned` / `own`) in §3 are not extra
 * capabilities — "view ticket detail" is one capability four roles hold, and
 * the difference between them is how many rows it reaches. The server applies
 * this independently of any query parameter, so a filter can narrow a result
 * set but never widen one; the client reads it only to label a screen honestly.
 */
export const ROW_SCOPES = ['organization', 'assigned', 'own'] as const
export type RowScope = (typeof ROW_SCOPES)[number]

export const TICKET_SCOPE_BY_ROLE: Record<UserRole, RowScope> = {
  admin: 'organization',
  manager: 'organization',
  agent: 'assigned',
  customer: 'own',
}

/** Roles that authenticate as an external party rather than as support staff. */
export const PORTAL_ROLES: readonly UserRole[] = ['customer']

export function isPortalRole(role: UserRole): boolean {
  return PORTAL_ROLES.includes(role)
}

/**
 * Whether `role` holds `permission` on the client's understanding of the matrix.
 *
 * An unrecognised role resolves to denied, matching `permissions_for` in the
 * Python module: the safe failure is a hidden button, never a shown one.
 */
export function can(role: UserRole | null | undefined, permission: Permission): boolean {
  if (!role) {
    return false
  }

  return ROLE_PERMISSIONS[role]?.has(permission) ?? false
}

/** Whether the role holds at least one of `permissions`. */
export function canAny(role: UserRole | null | undefined, permissions: readonly Permission[]): boolean {
  return permissions.some((permission) => can(role, permission))
}
