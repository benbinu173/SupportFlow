"""The SLA worker's queries — module-level functions, because there is no requester.

**Why this module exists at all.** Every other read in this application is a method on a
`TenantScopedRepository`, which cannot be constructed without a `TenantContext`. The SLA
sweep runs on a schedule: there is no request, no token, and no authenticated user. That
is not a gap to be worked around, it is the fact the whole file is arranged around.

**Why the context is not synthesised.** `TenantContext.user_id` is a required `uuid.UUID`
and its module argues at length that every field comes from a verified identity. A beat
task could invent a uuid and hand it to a repository, and the repository would happily
scope by the organization — but the value would mean "a user id that is not a user", and
`role` would have no honest value at all: it decides `permissions`, and there is no role
whose permissions describe "the scheduler". A fabricated identity is exactly the class of
thing ADR-009 and §4 exist against, and it would be fabricated inside the one component
that has no caller to blame for it.

**The precedent is already in this codebase, twice.** `audit_service.record` sits beside
`record_for` for precisely this reason — its docstring calls registration "the one call
site with no `TenantContext`" — and `user_repository.find_users_by_email_across_tenants`
sits beside `UserRepository` as a *module-level function rather than a method*, with the
reason spelled out: "so it cannot be reached for by accident while holding a scoped
repository." This module is the same shape applied to the second component that needs it.

**They live together, in one file, on purpose.** The value of "a function rather than a
method" is that the set of context-free queries stays small enough to count, and three
scattered across three repositories is not countable at a glance. Every function below is
one of them, each takes `organization_id` explicitly rather than reading it from anywhere,
and a reader auditing tenant isolation has one file to read. There is no class here and
there should not be one: a class implies state, and the only state any of these has is the
session they were handed.

**Nothing here is reachable from a request.** These functions are called by
`app/workers/sla_tasks.py` only. The API's equivalent reads are methods on
`SLAPolicyRepository`, `TicketEventRepository`, and `UserRepository`, which is what keeps
`organization_id` a value the *request* cannot influence — in this file it is a value no
request ever sees.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import SLA_ALERT_ROLES
from app.models.enums import TicketEventType, TicketPriority, TicketStatus
from app.models.sla_policy import SLAPolicy
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import User

# The two statuses that end a ticket's life, and therefore its clock. A tuple rather than
# a set so it can go straight into `not_in` without an ordering assumption, and named so
# the sweep and `find_pending` cannot disagree about what "still open" means.
TERMINAL_STATUSES = (TicketStatus.RESOLVED, TicketStatus.CLOSED)


async def load_policies(
    session: AsyncSession, organization_id: uuid.UUID
) -> dict[TicketPriority, SLAPolicy]:
    """This organization's active policies, keyed by priority.

    A dict rather than a sequence because every caller indexes it by the ticket's
    priority, and building the index once is what keeps a hundred-ticket sweep from being
    a hundred linear scans.

    Inactive rows are excluded here and not in the caller: `is_active = false` is the
    tenant saying "do not enforce a target for this priority", and the clock's answer for
    such a priority is `None` — the same answer as for a tenant that never configured one.
    """
    result = await session.execute(
        select(SLAPolicy).where(
            SLAPolicy.organization_id == organization_id,
            SLAPolicy.is_active.is_(True),
        )
    )
    return {policy.priority: policy for policy in result.scalars().all()}


async def find_pending(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    priority: TicketPriority,
    created_before: datetime,
    limit: int,
) -> Sequence[Ticket]:
    """Tickets at this priority whose clock could have produced an alert by now.

    `created_before` is the *earliest* instant at which any alert is due for a ticket at
    this priority — the response target's warning threshold — and the caller computes it
    with `sla_service.earliest_alert_offset`, because the clock is the service's and not
    this module's. One bound serves both timers: `resolution_time_minutes >=
    response_time_minutes` is a `CheckConstraint`, so the response warning can never land
    after the resolution warning, and a ticket the resolution timer considers overdue was
    therefore already inside this window.

    Terminal tickets are excluded, which is exactly the predicate
    `tickets.ix_tickets_sla_pending` is partial on — the index's comment reads "SLA sweep:
    scans unresolved tickets only", and it was written for this query in Phase D.

    `limit` bounds one run's work. A tenant with a hundred thousand overdue tickets should
    take several sweep cycles to work through them rather than one cycle that times out
    against the task's time limit and reports nothing. Ordered oldest first, so the
    tickets most overdue are the ones served.

    **This is a filter on what is worth saying, not on what is true.** `sla_service`
    computes the position of every ticket returned; that a ticket is here does not mean it
    has breached, only that it is old enough that it might have.
    """
    result = await session.execute(
        select(Ticket)
        .where(
            Ticket.organization_id == organization_id,
            Ticket.priority == priority,
            Ticket.status.not_in(TERMINAL_STATUSES),
            Ticket.created_at <= created_before,
        )
        .order_by(Ticket.created_at, Ticket.id)
        .limit(limit)
    )
    return result.scalars().all()


async def find_alerts(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    ticket_ids: Sequence[uuid.UUID],
) -> Sequence[TicketEvent]:
    """The SLA entries already on the timeline of these tickets.

    This is the sweep's idempotency guard, read as data rather than as a `NOT EXISTS` —
    because the same rows are also what the API reports as `warned_at` and `breached_at`,
    so fetching them once serves both and there is exactly one query.

    Filtered by `ticket_ids` rather than by a date range or a status, so the cost is
    bounded by the page the caller already decided to look at. Served by
    `ix_ticket_events_ticket_created`, which leads with `ticket_id`.

    Empty `ticket_ids` short-circuits: `IN ()` is not valid SQL, and a page of zero
    tickets is a real outcome in a tenant whose tickets are all fresh.
    """
    if not ticket_ids:
        return []

    result = await session.execute(
        select(TicketEvent)
        .where(
            TicketEvent.organization_id == organization_id,
            TicketEvent.ticket_id.in_(ticket_ids),
            TicketEvent.event_type.in_((TicketEventType.SLA_WARNING, TicketEventType.SLA_BREACHED)),
        )
        .order_by(TicketEvent.created_at, TicketEvent.id)
    )
    return result.scalars().all()


async def find_manager_ids(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[uuid.UUID]:
    """Every active manager's id. §27's "notify agents/managers".

    Active only, for the reason `notification_service._portal_users_for` drops inactive
    portal accounts: a notification addressed to a deactivated account is one nobody can
    read, nobody will mark read, and nobody will receive the email for — the delivery task
    refuses it — so writing the row would produce an alert with no recipient and a badge
    that never clears.

    Managers specifically and not admins: §3's matrix is what draws this line, giving
    admins every capability but describing the queue as the manager's job. An admin who
    also wants the alerts is one row away from being a manager, and fanning out to every
    role that could act would make the alert set indistinguishable from the notification
    centre. **Which roles those are is `SLA_ALERT_ROLES`, in `core/permissions.py`**, and
    is named rather than compared here so that the decision lives beside the matrix that
    decides what owning the queue means.
    """
    result = await session.execute(
        select(User.id)
        .where(
            User.organization_id == organization_id,
            User.role.in_(SLA_ALERT_ROLES),
            User.is_active.is_(True),
        )
        .order_by(User.created_at, User.id)
    )
    return result.scalars().all()
