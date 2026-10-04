"""Tenant context — the identity every request is served as.

This is the object that makes multi-tenancy enforceable. Everything downstream that
touches tenant-owned data takes one of these, and a repository cannot be constructed
without it, so "forgot to filter by organization" becomes a type error rather than a
data leak.

The rules it encodes, from architecture §4 and §11:

* `organization_id` is **derived from the authenticated user**, never from the request
  body, query string, or a header. A client cannot name the tenant it wants.
* The role is the one stored in the database at request time, not the one baked into
  an access token up to 15 minutes ago — so a demotion takes effect immediately.
* Permissions are resolved once, here, so no downstream code re-derives them from the
  role string and no two call sites can disagree.
* For a portal caller, the `Customer` row they act as comes from their user row too —
  so "my tickets" is resolved from identity, never from a request parameter.

Frozen because handing a mutable identity object down the call stack invites exactly
one bug: something mutates it. `dataclasses.replace` covers the legitimate cases.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.core.permissions import Permission, RowScope, permissions_for
from app.models.enums import UserRole


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Who is calling, which tenant they belong to, and what they may do."""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: UserRole

    # Set only for a portal caller — a user in `PORTAL_ROLES`, linked to the `Customer`
    # row they are acting as. It is what makes `RowScope.OWN` resolvable: "tickets this
    # customer raised" needs a customer, and the alternative — looking one up by email
    # at query time — would make ownership an inference rather than a fact.
    #
    # `None` for staff, and `None` for a portal account that has not been linked. Both
    # cases mean the same thing to a scoped query, and the repository reads it as
    # "matches nothing" rather than "matches everything" (ADR-015).
    customer_id: uuid.UUID | None = None

    # The caller's email, copied from their user row. **Not an authorization input** —
    # nothing in this codebase reads it to decide anything, and `has()` and `scope_for`
    # cannot see it. It is here because `AppError`s are logged with the user id and
    # `audit_logs` denormalizes the actor's email so a row survives its actor's deletion
    # (`actor_user_id` is `SET NULL`), and carrying it on the one object that already
    # represents "who is calling" beats threading a second parameter through every
    # audited call site.
    email: str | None = None

    # Not a constructor parameter (`init=False`) so it cannot be supplied by a
    # caller. A `permissions=` argument would be a way for any code path to grant
    # itself capabilities; deriving it from `role` in __post_init__ means the only
    # route to a permission is to hold the role that carries it.
    permissions: frozenset[Permission] = field(init=False, default=frozenset(), repr=False)

    def __post_init__(self) -> None:
        # object.__setattr__ because the dataclass is frozen. This is the documented
        # way to compute a frozen field, not a workaround.
        object.__setattr__(self, "permissions", permissions_for(self.role))

    def has(self, *permissions: Permission) -> bool:
        """Whether the caller holds **all** of the given permissions.

        All, not any: a route guard names one capability at a time, and if one ever
        needs two, requiring both is the fail-closed reading. Callers who want either
        can call this twice.
        """
        return all(permission in self.permissions for permission in permissions)

    def scope_for(self, scopes: Mapping[UserRole, RowScope]) -> RowScope:
        """This caller's row scope for the resource `scopes` describes.

        Takes the mapping, not a resource name, so the caller's intent is visible at
        the call site. Repositories call this with e.g. `TICKET_SCOPE_BY_ROLE`.
        """
        return scopes.get(self.role, RowScope.OWN)

    def __repr__(self) -> str:
        # No permissions in the repr: this ends up in log lines, and a 38-element set
        # per request buries the two ids that matter.
        return f"<TenantContext user={self.user_id} org={self.organization_id} role={self.role}>"


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """The tenant a background task acts for. **Carries no authority.**

    A Celery task has no request and so no `TenantContext` — but the work it does is still
    tenant-owned, and `AIUsage.organization_id` has to say which tenant paid for a call.
    This is that value and nothing else.

    **What it deliberately does not have is the point.** No `role`, no `permissions`, no
    `has()`, no `scope_for()`. It cannot answer an authorization question because it cannot
    be asked one, so a task cannot grant itself a capability by holding one — the guarantee
    is structural rather than a convention somebody has to remember. `sla_repository.py`
    refused to invent a `TenantContext` in Phase Q for exactly this reason: *"`role` would
    have no honest value at all: it decides `permissions`, and there is no role whose
    permissions describe 'the scheduler'."* A fabricated identity is the class of thing
    ADR-009 and §4 exist against, and this is the honest alternative — a value that says
    "this tenant" and is silent on "as whom".

    It follows that `TenantScopedRepository` does not accept one. A worker reaches the
    database through module-level functions that take `organization_id` explicitly
    (`app/repositories/sla_repository.py`, `app/repositories/ai_repository.py`), which keeps
    the set of context-free queries small enough to count — in one place, per the reasoning
    those modules give. It also writes its own timeline entries rather than calling
    `ticket_service.record_event`, which needs a `TenantContext` to name its actor;
    `app/workers/sla_tasks.py` made that call in Phase Q and its comment already names
    *"completed AI analyses"* among the events with no actor.

    **One consumer, and that is the whole list:** `app/services/ai_service.py`, which takes
    it so the ledger row can say which tenant the spend belongs to. Passing a bare
    `uuid.UUID` there would compile and would work, and would give up the property that
    makes a wrong tenant unpassable — a `WorkerContext` is a *type* that says "this tenant,
    no caller", and an id says nothing at all.
    """

    organization_id: uuid.UUID

    def __repr__(self) -> str:
        return f"<WorkerContext org={self.organization_id}>"


@dataclass(frozen=True, slots=True)
class RequestOrigin:
    """Where a request came from, as far as the socket and the headers can say.

    Lives here rather than in `app/api/deps.py` because the audit service needs the
    type, and a service importing from the API layer would invert the dependency the
    rest of the project keeps straight. It sits beside `TenantContext` because it is the
    same kind of object: per-request context derived once and passed down.

    **Both fields are client-controlled and neither is used for a decision.** The IP is
    the peer address, which a client cannot forge but a reverse proxy collapses; the
    user agent is a header the client writes freely. They exist so an audit row can
    answer "where did this come from" during an investigation — a question asked after
    the fact, by a person, and never by the code.
    """

    ip_address: str | None = None
    user_agent: str | None = None
