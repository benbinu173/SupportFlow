"""Authorization: the permission vocabulary and the role→permission mapping.

Every authorization decision in the application resolves through this module. Phase G
requires "centralized permission checks" and explicitly forbids scattering role
strings through the codebase, so no other module compares a `UserRole` directly — a
test in `tests/unit/test_permissions.py` enforces that mechanically.

Two concepts, kept deliberately apart
-------------------------------------

**Capability** — *may this role perform this action at all?* That is what `Permission`
and `ROLE_PERMISSIONS` answer, and it is all a route-level dependency can decide.

**Scope** — *which rows may the action touch?* `docs/requirements.md` §3 qualifies
several rows with `own` or `assigned`. Those qualifiers are not extra capabilities:
"view ticket detail" is one capability that four roles hold, and the difference
between them is how many rows it reaches. Scope can only be applied where the query is
built, so it is a separate mapping consumed by repositories.

Collapsing the two would mean three permission names for one capability
(`TICKET_VIEW_ALL` / `_ASSIGNED` / `_OWN`), every route check multiplying, and a
`ROLE_PERMISSIONS` table that no longer lines up row-for-row with the documented
matrix. Keeping them separate means the matrix can be asserted against this file
directly — which `tests/unit/test_permissions.py` does, role by role.
"""

from collections.abc import Mapping
from enum import StrEnum

from app.models.enums import UserRole


class Permission(StrEnum):
    """One member per capability row in `docs/requirements.md` §3.

    Values are namespaced (`resource:action`) so a permission is readable in a log
    line or an error payload without needing its Python name.
    """

    # --- Organization -----------------------------------------------------
    ORG_VIEW = "org:view"
    ORG_UPDATE = "org:update"

    # --- Users ------------------------------------------------------------
    USER_LIST = "user:list"
    USER_CREATE = "user:create"
    USER_UPDATE_ROLE = "user:update_role"
    USER_DEACTIVATE = "user:deactivate"
    # Every role can read its own profile, including customer.
    PROFILE_VIEW = "profile:view"

    # --- Customers --------------------------------------------------------
    CUSTOMER_LIST = "customer:list"
    CUSTOMER_CREATE = "customer:create"
    CUSTOMER_UPDATE = "customer:update"

    # --- Tickets ----------------------------------------------------------
    # Scope-qualified in the matrix (all/assigned/own) but a single capability here;
    # see TICKET_SCOPE_BY_ROLE for the row restriction.
    TICKET_LIST = "ticket:list"
    TICKET_CREATE = "ticket:create"
    TICKET_VIEW = "ticket:view"
    TICKET_ASSIGN = "ticket:assign"
    TICKET_CHANGE_PRIORITY = "ticket:change_priority"
    TICKET_CHANGE_STATUS = "ticket:change_status"
    TICKET_CLOSE = "ticket:close"
    TICKET_REOPEN = "ticket:reopen"

    # --- Messages ---------------------------------------------------------
    MESSAGE_READ_PUBLIC = "message:read_public"
    MESSAGE_READ_INTERNAL = "message:read_internal"
    MESSAGE_POST_REPLY = "message:post_reply"
    MESSAGE_POST_INTERNAL = "message:post_internal"

    # --- Attachments ------------------------------------------------------
    ATTACHMENT_UPLOAD = "attachment:upload"
    ATTACHMENT_DOWNLOAD = "attachment:download"

    # --- AI ---------------------------------------------------------------
    AI_REQUEST_ANALYSIS = "ai:request_analysis"
    AI_REQUEST_SUGGESTION = "ai:request_suggestion"
    AI_QUERY_KNOWLEDGE = "ai:query_knowledge"
    AI_CONFIGURE = "ai:configure"
    AI_VIEW_USAGE = "ai:view_usage"

    # --- Knowledge base ---------------------------------------------------
    KB_LIST = "kb:list"
    KB_UPLOAD = "kb:upload"
    KB_DELETE = "kb:delete"

    # --- SLA --------------------------------------------------------------
    SLA_VIEW = "sla:view"
    SLA_CONFIGURE = "sla:configure"

    # --- Analytics --------------------------------------------------------
    ANALYTICS_ORG = "analytics:org"
    ANALYTICS_OWN = "analytics:own"

    # --- Audit ------------------------------------------------------------
    AUDIT_VIEW = "audit:view"

    # --- Notifications ----------------------------------------------------
    NOTIFICATION_LIST = "notification:list"


class RowScope(StrEnum):
    """How many rows within the tenant an actor's query may reach.

    `ORGANIZATION` — every row in the tenant.
    `ASSIGNED` — rows attached to the actor (tickets assigned to this agent).
    `OWN` — rows the actor owns (tickets this customer raised).
    """

    ORGANIZATION = "organization"
    ASSIGNED = "assigned"
    OWN = "own"


# ---------------------------------------------------------------------------
# The matrix, transcribed from docs/requirements.md §3
# ---------------------------------------------------------------------------
# Read this table against that one; `tests/unit/test_permissions.py` does exactly
# that, so the two cannot drift. Held as explicit sets rather than built up by
# inheritance between roles: an admin's permissions are not "a manager's plus more",
# and writing them out makes an accidental grant visible in review.

_ADMIN_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

_MANAGER_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.ORG_VIEW,  # but not ORG_UPDATE
        Permission.USER_LIST,  # but not create / update role / deactivate
        Permission.PROFILE_VIEW,
        Permission.CUSTOMER_LIST,
        Permission.CUSTOMER_CREATE,
        Permission.CUSTOMER_UPDATE,
        Permission.TICKET_LIST,
        Permission.TICKET_CREATE,
        Permission.TICKET_VIEW,
        Permission.TICKET_ASSIGN,
        Permission.TICKET_CHANGE_PRIORITY,
        Permission.TICKET_CHANGE_STATUS,
        Permission.TICKET_CLOSE,
        Permission.TICKET_REOPEN,
        Permission.MESSAGE_READ_PUBLIC,
        Permission.MESSAGE_READ_INTERNAL,
        Permission.MESSAGE_POST_REPLY,
        Permission.MESSAGE_POST_INTERNAL,
        Permission.ATTACHMENT_UPLOAD,
        Permission.ATTACHMENT_DOWNLOAD,
        Permission.AI_REQUEST_ANALYSIS,
        Permission.AI_REQUEST_SUGGESTION,
        Permission.AI_QUERY_KNOWLEDGE,
        Permission.AI_VIEW_USAGE,  # but not AI_CONFIGURE
        Permission.KB_LIST,  # but not upload / delete
        Permission.SLA_VIEW,  # but not SLA_CONFIGURE
        Permission.ANALYTICS_ORG,
        Permission.ANALYTICS_OWN,
        Permission.NOTIFICATION_LIST,
    }
)

_AGENT_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.PROFILE_VIEW,
        Permission.CUSTOMER_LIST,
        Permission.CUSTOMER_CREATE,
        Permission.CUSTOMER_UPDATE,
        # Ticket and message capabilities are held, but scoped to assigned work —
        # see the scope maps below. An agent cannot assign, reprioritise, or act on
        # a ticket that is not theirs.
        Permission.TICKET_LIST,
        Permission.TICKET_CREATE,
        Permission.TICKET_VIEW,
        Permission.TICKET_CHANGE_STATUS,
        Permission.TICKET_CLOSE,
        Permission.TICKET_REOPEN,
        Permission.MESSAGE_READ_PUBLIC,
        Permission.MESSAGE_READ_INTERNAL,
        Permission.MESSAGE_POST_REPLY,
        Permission.MESSAGE_POST_INTERNAL,
        Permission.ATTACHMENT_UPLOAD,
        Permission.ATTACHMENT_DOWNLOAD,
        Permission.AI_REQUEST_ANALYSIS,
        Permission.AI_REQUEST_SUGGESTION,
        Permission.AI_QUERY_KNOWLEDGE,
        Permission.KB_LIST,
        Permission.SLA_VIEW,
        Permission.ANALYTICS_OWN,  # own performance only
        Permission.NOTIFICATION_LIST,
    }
)

_CUSTOMER_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.PROFILE_VIEW,
        # Scoped to their own tickets. No internal notes, no customer directory, no
        # AI, no knowledge base, no analytics.
        Permission.TICKET_LIST,
        Permission.TICKET_CREATE,
        Permission.TICKET_VIEW,
        Permission.TICKET_CLOSE,
        Permission.TICKET_REOPEN,
        Permission.MESSAGE_READ_PUBLIC,
        Permission.MESSAGE_POST_REPLY,
        Permission.ATTACHMENT_UPLOAD,
        Permission.ATTACHMENT_DOWNLOAD,
        Permission.NOTIFICATION_LIST,
    }
)

ROLE_PERMISSIONS: Mapping[UserRole, frozenset[Permission]] = {
    UserRole.ADMIN: _ADMIN_PERMISSIONS,
    UserRole.MANAGER: _MANAGER_PERMISSIONS,
    UserRole.AGENT: _AGENT_PERMISSIONS,
    UserRole.CUSTOMER: _CUSTOMER_PERMISSIONS,
}


# ---------------------------------------------------------------------------
# Row scope, by resource and role
# ---------------------------------------------------------------------------
# Tickets, messages, and attachments share one visibility rule: a message is reachable
# exactly when its ticket is, and the same for an attachment. They are separate
# mappings rather than one, so that a later divergence (an agent who may read any
# attachment but only assigned tickets) is a one-line change instead of a redesign.

_ORGANIZATION_ROW = RowScope.ORGANIZATION

TICKET_SCOPE_BY_ROLE: Mapping[UserRole, RowScope] = {
    UserRole.ADMIN: _ORGANIZATION_ROW,
    UserRole.MANAGER: _ORGANIZATION_ROW,
    UserRole.AGENT: RowScope.ASSIGNED,
    UserRole.CUSTOMER: RowScope.OWN,
}

MESSAGE_SCOPE_BY_ROLE: Mapping[UserRole, RowScope] = dict(TICKET_SCOPE_BY_ROLE)

ATTACHMENT_SCOPE_BY_ROLE: Mapping[UserRole, RowScope] = dict(TICKET_SCOPE_BY_ROLE)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def permissions_for(role: UserRole) -> frozenset[Permission]:
    """Every capability the role holds.

    Unknown roles get an empty set. `UserRole` is a closed enum backed by a database
    type, so this is unreachable in practice — and if it ever became reachable, the
    safe failure is "denied", not "allowed".
    """
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: UserRole, permission: Permission) -> bool:
    """Whether `role` holds `permission`."""
    return permission in permissions_for(role)


def row_scope_for(role: UserRole, scopes: Mapping[UserRole, RowScope]) -> RowScope:
    """Resolve a role's row scope for one resource.

    Takes the mapping explicitly rather than a resource name, so the caller's choice
    of resource is visible at the call site instead of hidden behind a string key.
    Falls back to `OWN` — the narrowest scope — for an unrecognised role.
    """
    return scopes.get(role, RowScope.OWN)
