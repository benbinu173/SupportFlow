"""Ticket core: creation, the queue, and the lifecycle.

Three things live here that a route cannot express:

* **Row scope.** An agent's queue is the tickets assigned to them and a customer's is
  the tickets they raised. The repository applies that; this layer is where the ticket
  is resolved before anything else touches it, so every other operation in this module
  is operating on a ticket the caller was already allowed to see.

* **The lifecycle.** Spec §5: "Status is never mutated by a blind field update;
  transitions go through an action that validates the edge, records a ticket event, and
  audits where appropriate." `can_transition` is the table; `_transition` is the only
  code that writes `Ticket.status`, and it writes an event in the same transaction.

* **The invariants a capability cannot state.** A ticket must belong to a customer in
  the caller's own organization; a portal caller does not get to choose whose ticket
  they are raising; priority is not a customer's decision.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ErrorCode,
    InvalidTicketTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.permissions import TICKET_SCOPE_BY_ROLE, Permission, RowScope
from app.core.tenancy import RequestOrigin, TenantContext
from app.models.enums import (
    AuditAction,
    TicketEventType,
    TicketPriority,
    TicketStatus,
    can_transition,
)
from app.models.notification import Notification
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.repositories.customer_repository import CustomerRepository
from app.repositories.ticket_event_repository import TicketEventRepository
from app.repositories.ticket_repository import TicketRepository
from app.repositories.user_repository import UserRepository
from app.schemas.fields import SortOrder
from app.schemas.ticket import (
    TicketCreate,
    TicketPriorityUpdate,
    TicketSortKey,
    TicketStatusUpdate,
)
from app.services import audit_service, notification_service

logger = structlog.get_logger(__name__)

# The edges `POST /tickets/{id}/status` will perform. The three it excludes have their
# own routes and their own capabilities — assignment, closing, and reopening — so that
# the capability a request needs is a property of the endpoint rather than of its body.
# Each exclusion carries the hint the caller gets, because "invalid transition" on its
# own leaves a client guessing which endpoint it wanted.
_ACTION_EDGES: dict[TicketStatus, str] = {
    TicketStatus.ASSIGNED: "Use POST /tickets/{ticket_id}/assign to assign an agent.",
    TicketStatus.CLOSED: "Use POST /tickets/{ticket_id}/close to close a resolved ticket.",
    TicketStatus.OPEN: "Use POST /tickets/{ticket_id}/reopen to reopen a closed ticket.",
}


async def list_tickets(
    session: AsyncSession,
    context: TenantContext,
    *,
    status: TicketStatus | None,
    priority: TicketPriority | None,
    assigned_agent_id: uuid.UUID | None,
    unassigned: bool,
    customer_id: uuid.UUID | None,
    term: str | None,
    created_after: datetime | None,
    created_before: datetime | None,
    sort: TicketSortKey,
    order: SortOrder,
    limit: int,
    offset: int,
) -> list[Ticket]:
    """A page of tickets the caller may reach, filtered and searched.

    Every parameter is passed through rather than defaulted here: the route is where a
    query parameter's default belongs, and a service that also had an opinion about it
    would be a second place for the two to disagree. `include_internal` is the exception
    and is *not* a parameter — it is read from the context inside the repository, so no
    caller can ask for the internal-note arm of a search.

    Increasingly, this function's body is the answer to "where does a ticket list come
    from" and nothing else. That is deliberate: the parameters are a contract with the
    route, and any logic added here would be logic the repository cannot see.
    """
    return list(
        await TicketRepository(session, context).list_tickets(
            status=status,
            priority=priority,
            assigned_agent_id=assigned_agent_id,
            unassigned=unassigned,
            customer_id=customer_id,
            term=term,
            created_after=created_after,
            created_before=created_before,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
    )


async def get_ticket(session: AsyncSession, context: TenantContext, ticket_id: uuid.UUID) -> Ticket:
    """One ticket the caller may reach, or 404.

    A ticket in another organization and a ticket assigned to a colleague produce the
    identical 404. The second is worth stating plainly: an agent should not be able to
    enumerate the support desk's workload by watching which ids are refused and which
    are not.
    """
    return await require_visible_ticket(session, context, ticket_id)


async def list_events(
    session: AsyncSession, context: TenantContext, ticket_id: uuid.UUID
) -> list[TicketEvent]:
    """A ticket's activity timeline.

    Resolves the ticket first, so the scope check is the same one `get_ticket` uses —
    there is no route to a ticket's timeline that does not go through its ticket.

    `INTERNAL_NOTE_ADDED` entries are included only for a caller holding
    `MESSAGE_READ_INTERNAL`, which is the capability that governs internal notes
    everywhere else. The timeline is one more place they would otherwise be disclosed:
    a customer who can see that a note was written at 14:02, and not what it said, has
    still learned something the thread filter is at pains to hide.
    """
    await require_visible_ticket(session, context, ticket_id)
    return list(
        await TicketEventRepository(session, context).list_for_ticket(
            ticket_id, include_internal=context.has(Permission.MESSAGE_READ_INTERNAL)
        )
    )


async def create_ticket(
    session: AsyncSession,
    context: TenantContext,
    payload: TicketCreate,
    *,
    origin: RequestOrigin | None = None,
) -> Ticket:
    """Raise a ticket.

    Two callers, two rules for `customer_id`, and neither is guessable from the payload:

    * **Staff** must name the customer. A ticket without one cannot be stored — the
      column is non-nullable — and defaulting it to the caller would be wrong, since a
      support agent is not the customer.
    * **A portal caller** must *not* name one. It comes from their own user row. A
      customer sending someone else's `customer_id` is refused rather than overridden:
      silently ignoring it would leave them believing they raised a ticket for someone
      else, and the two readings of an ignored field are both wrong.

    `priority` is likewise conditional on `TICKET_CHANGE_PRIORITY`. A customer raising
    an urgent ticket does not get to decide that it is urgent.
    """
    tickets = TicketRepository(session, context)

    if context.scope_for(TICKET_SCOPE_BY_ROLE) is RowScope.OWN:
        if payload.customer_id is not None:
            raise ValidationError("A portal caller cannot raise a ticket for another customer.")
        if context.customer_id is None:
            # Reachable only if a portal account was stored without its customer link.
            # Refused rather than defaulted because there is no sensible customer to
            # fall back to, and a ticket nobody can see is worse than an error.
            raise ValidationError("This account is not linked to a customer record.")
        customer_id = context.customer_id
    else:
        if payload.customer_id is None:
            raise ValidationError("customer_id is required.")
        if not await _customer_is_visible(session, context, payload.customer_id):
            raise NotFoundError(ErrorCode.CUSTOMER_NOT_FOUND)
        customer_id = payload.customer_id

    if payload.priority is not None and not context.has(Permission.TICKET_CHANGE_PRIORITY):
        logger.info(
            "permission_denied",
            user_id=str(context.user_id),
            organization_id=str(context.organization_id),
            role=context.role.value,
            required=[str(Permission.TICKET_CHANGE_PRIORITY)],
        )
        raise PermissionDeniedError("You cannot set a ticket's priority.")

    ticket = Ticket(
        organization_id=context.organization_id,
        # Allocated under a lock held until this transaction commits. See
        # `TicketRepository.allocate_number` and ADR-016.
        number=await tickets.allocate_number(),
        customer_id=customer_id,
        subject=payload.subject,
        description=payload.description,
        category=payload.category,
        status=TicketStatus.OPEN,
        priority=payload.priority or TicketPriority.MEDIUM,
    )
    tickets.add(ticket)

    # Flushed, not committed, so the id exists for the event below and both rows land
    # in one transaction. A ticket without its CREATED event is a timeline that starts
    # mid-story.
    await session.flush()
    record_event(
        session,
        context,
        ticket,
        TicketEventType.CREATED,
        to_value=TicketStatus.OPEN.value,
    )
    audit_service.record_for(
        session,
        context,
        AuditAction.TICKET_CREATED,
        target_type="ticket",
        target_id=ticket.id,
        # The subject is the one field that makes a trail read as a story rather than a
        # list of ids. It carries no `before` — there was no earlier ticket to describe,
        # which is what an absent `before` key means throughout the trail.
        after={"number": ticket.number, "status": ticket.status.value},
        metadata={"subject": ticket.subject},
        origin=origin,
    )
    await session.commit()

    logger.info(
        "ticket_created",
        ticket_id=str(ticket.id),
        ticket_number=ticket.number,
        organization_id=str(context.organization_id),
        customer_id=str(ticket.customer_id),
        actor_id=str(context.user_id),
    )
    return ticket


async def assign_ticket(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    *,
    assigned_agent_id: uuid.UUID | None,
    origin: RequestOrigin | None = None,
) -> Ticket:
    """Assign, reassign, or unassign a ticket.

    **Assigning an `OPEN` ticket moves it to `ASSIGNED`** — that is the one lifecycle
    edge assignment owns, and doing it here rather than in `/status` is what stops a
    ticket existing in status `ASSIGNED` with nobody assigned to it. Reassignment
    between agents leaves the status alone.

    **Unassigning an `ASSIGNED` ticket is refused.** `ASSIGNED` *means* "assigned", so
    clearing the agent while keeping the status is incoherent — and the documented
    lifecycle has no edge back to `OPEN`. From any other status the agent field is
    independent of the status, so clearing it is allowed.
    """
    ticket = await require_visible_ticket(session, context, ticket_id)

    if assigned_agent_id is not None and not await _agent_is_visible(
        session, context, assigned_agent_id
    ):
        raise NotFoundError(ErrorCode.USER_NOT_FOUND)
    previous = ticket.assigned_agent_id

    if assigned_agent_id is None and ticket.status is TicketStatus.ASSIGNED:
        raise InvalidTicketTransitionError(
            TicketStatus.ASSIGNED.value,
            TicketStatus.OPEN.value,
            "A ticket in ASSIGNED status has an agent by definition. Move it to "
            "IN_PROGRESS, or assign a different agent.",
        )

    # Nothing changed: no write, no event. A reassignment to the same agent would
    # otherwise fill the timeline with entries recording that nothing happened.
    if previous == assigned_agent_id:
        return ticket

    ticket.assigned_agent_id = assigned_agent_id

    if assigned_agent_id is not None and ticket.status is TicketStatus.OPEN:
        ticket.status = TicketStatus.ASSIGNED

    event = record_event(
        session,
        context,
        ticket,
        TicketEventType.ASSIGNED if assigned_agent_id else TicketEventType.UNASSIGNED,
        from_value=str(previous) if previous else None,
        to_value=str(assigned_agent_id) if assigned_agent_id else None,
    )
    audit_service.record_for(
        session,
        context,
        # §34's list has no "unassigned" action, and a ticket going back on the queue is
        # a change to the same field `TICKET_ASSIGNED` names. A distinct member would
        # mean a migration to record the same field going in the other direction.
        AuditAction.TICKET_ASSIGNED,
        target_type="ticket",
        target_id=ticket.id,
        # Both ends as ids, or `None` when the field was empty. The difference between
        # "never assigned" and "assigned to nobody" is the whole content of this record.
        before={"assigned_agent_id": str(previous) if previous else None},
        after={"assigned_agent_id": str(assigned_agent_id) if assigned_agent_id else None},
        origin=origin,
    )
    # Staged into this transaction, delivered after it. The notification is a claim that
    # the assignment happened, so it has to be written by the same unit of work that made
    # it true — and the task that emails it reads the row by id, so it cannot be queued
    # until that row is committed. See `app/services/notification_service.py`.
    notifications = await notification_service.notify_for_event(session, context, ticket, event)
    await session.commit()
    notification_service.enqueue_delivery(notifications)

    logger.info(
        "ticket_assigned" if assigned_agent_id else "ticket_unassigned",
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        assigned_agent_id=str(assigned_agent_id) if assigned_agent_id else None,
        previous_agent_id=str(previous) if previous else None,
        actor_id=str(context.user_id),
    )
    return ticket


async def change_priority(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    payload: TicketPriorityUpdate,
    *,
    origin: RequestOrigin | None = None,
) -> Ticket:
    """Change the effective business priority.

    `ai_recommended_priority` is deliberately untouched. Spec §6 keeps the model's
    suggestion and the business decision in separate columns so model accuracy stays
    measurable — a manual override that overwrote the recommendation would destroy the
    comparison it exists for.
    """
    ticket = await require_visible_ticket(session, context, ticket_id)

    if ticket.priority is payload.priority:
        return ticket

    previous = ticket.priority
    ticket.priority = payload.priority
    record_event(
        session,
        context,
        ticket,
        TicketEventType.PRIORITY_CHANGED,
        from_value=previous.value,
        to_value=payload.priority.value,
    )
    audit_service.record_for(
        session,
        context,
        AuditAction.TICKET_PRIORITY_CHANGED,
        target_type="ticket",
        target_id=ticket.id,
        before={"priority": previous.value},
        after={"priority": payload.priority.value},
        origin=origin,
    )
    await session.commit()

    logger.info(
        "ticket_priority_changed",
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        previous_priority=previous.value,
        priority=payload.priority.value,
        actor_id=str(context.user_id),
    )
    return ticket


async def change_status(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    payload: TicketStatusUpdate,
    *,
    origin: RequestOrigin | None = None,
) -> Ticket:
    """Move a ticket between the working states.

    Assignment, closing, and reopening have their own endpoints and are refused here
    with a pointer to the right one — see `_ACTION_EDGES`. The effect is that
    `POST /status` performs exactly the four middle edges (`ASSIGNED → IN_PROGRESS`,
    `IN_PROGRESS → WAITING_FOR_CUSTOMER`, `IN_PROGRESS → RESOLVED`,
    `WAITING_FOR_CUSTOMER → IN_PROGRESS`) and the capability a request needs is always
    a property of the endpoint rather than of its body.
    """
    ticket = await require_visible_ticket(session, context, ticket_id)

    hint = _ACTION_EDGES.get(payload.status)
    if hint is not None:
        raise InvalidTicketTransitionError(ticket.status.value, payload.status.value, hint)

    notifications = await _transition(session, context, ticket, payload.status, origin=origin)
    await session.commit()
    notification_service.enqueue_delivery(notifications)

    logger.info(
        "ticket_status_changed",
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        status=ticket.status.value,
        actor_id=str(context.user_id),
    )
    return ticket


async def close_ticket(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    *,
    origin: RequestOrigin | None = None,
) -> Ticket:
    """Close a resolved ticket. `RESOLVED → CLOSED`, and nothing else.

    Spec §3's row is "confirm resolution / close own ticket", which is a customer — or
    a manager — agreeing that the fix worked. That is a different act from an agent
    deciding the ticket is resolved, which is `POST /status` with `resolved`. Keeping
    the two apart is why this endpoint accepts only the one edge: a caller trying to
    close an `IN_PROGRESS` ticket is told `409`, not quietly walked through two
    transitions that would skip the customer's confirmation entirely.
    """
    ticket = await require_visible_ticket(session, context, ticket_id)

    notifications = await _transition(session, context, ticket, TicketStatus.CLOSED, origin=origin)
    await session.commit()
    notification_service.enqueue_delivery(notifications)

    logger.info(
        "ticket_closed",
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        actor_id=str(context.user_id),
    )
    return ticket


async def reopen_ticket(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    *,
    origin: RequestOrigin | None = None,
) -> Ticket:
    """Reopen a closed ticket. `CLOSED → OPEN`.

    **The assignment is cleared.** A reopened ticket goes back on the queue rather than
    to whoever had it before, because `OPEN` and `ASSIGNED` are the lifecycle's two
    ways of saying "nobody owns this" and "somebody does" — a ticket in `OPEN` with an
    agent set is a state the rest of the model has no reading for. It would also break
    assignment: assigning it back to that same agent is a no-op, so the ticket could
    never reach `ASSIGNED` again and would sit in `OPEN` forever.

    The history is not lost by clearing anything here: it lives in `ticket_events`, and
    the reopened ticket's timeline still shows who worked it and what they did.

    `resolved_at` and `closed_at` are cleared for the same kind of reason. They
    describe the ticket's *current* state, and leaving them populated would make every
    SLA and duration query wrong.
    """
    ticket = await require_visible_ticket(session, context, ticket_id)

    notifications = await _transition(
        session,
        context,
        ticket,
        TicketStatus.OPEN,
        event_type=TicketEventType.REOPENED,
        origin=origin,
    )
    ticket.resolved_at = None
    ticket.closed_at = None

    previous_agent = ticket.assigned_agent_id
    ticket.assigned_agent_id = None

    await session.commit()
    notification_service.enqueue_delivery(notifications)

    logger.info(
        "ticket_reopened",
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        previous_agent_id=str(previous_agent) if previous_agent else None,
        actor_id=str(context.user_id),
    )
    return ticket


# ---------------------------------------------------------------------------
# The transition machinery
# ---------------------------------------------------------------------------


async def _transition(
    session: AsyncSession,
    context: TenantContext,
    ticket: Ticket,
    target: TicketStatus,
    *,
    event_type: TicketEventType = TicketEventType.STATUS_CHANGED,
    origin: RequestOrigin | None = None,
) -> list[Notification]:
    """Validate one lifecycle edge and apply it. **The only writer of `Ticket.status`.**

    Kept as a plain function that mutates and records without committing, so the caller
    owns the transaction — `close_ticket` needs to clear a timestamp in the same unit of
    work, and a helper that committed would make that impossible.

    The terminal timestamps are set in the same statement as the status, because a
    `CheckConstraint` on the table requires `resolved_at` for a `resolved` row and
    `closed_at` implies it too. Forgetting is an integrity error rather than a ticket
    that claims to be resolved and has no resolution time.

    **The audit row is written here for the same reason the status is.** Being the only
    writer makes this the only place that knows both ends of the change, so it is also
    the only place that can report it without guessing — the property ADR-017 leans on,
    applied to the trail rather than to the lifecycle.

    **Async because a resolution also notifies the customer.** §26's "ticket resolved"
    has a recipient who is not the person clicking, and resolving that recipient means a
    lookup. Putting it here rather than in `change_status` follows the same argument as
    the audit row: hooking the one route that resolves a ticket today would work today
    and would silently stop the day a second route did.

    Returns the staged notifications; the caller delivers them after its commit.
    """
    current = ticket.status
    if not can_transition(current, target):
        raise InvalidTicketTransitionError(current.value, target.value)

    ticket.status = target

    if target is TicketStatus.RESOLVED or target is TicketStatus.CLOSED:
        now = datetime.now(UTC)
        if target is TicketStatus.RESOLVED:
            ticket.resolved_at = now
        else:
            ticket.closed_at = now
            # Set only if absent. Overwriting would replace the real resolution time
            # with the moment someone clicked close, which is a different fact and the
            # one SLA reporting needs.
            if ticket.resolved_at is None:
                ticket.resolved_at = now

    event = record_event(
        session,
        context,
        ticket,
        event_type,
        from_value=current.value,
        to_value=target.value,
    )

    # §34 names reopening and resolution separately from a plain status change, and
    # the difference is the *action*, not the status it lands on: `CLOSED -> OPEN` via
    # `/reopen` is "reopened", while `IN_PROGRESS -> RESOLVED` via `/status` is
    # "resolved". Both would otherwise collapse into `TICKET_STATUS_CHANGED`, and a
    # trail whose entries all read the same is one nobody can filter.
    if event_type is TicketEventType.REOPENED:
        action = AuditAction.TICKET_REOPENED
    elif target is TicketStatus.RESOLVED:
        action = AuditAction.TICKET_RESOLVED
    else:
        action = AuditAction.TICKET_STATUS_CHANGED

    audit_service.record_for(
        session,
        context,
        action,
        target_type="ticket",
        target_id=ticket.id,
        before={"status": current.value},
        after={"status": target.value},
        origin=origin,
    )

    # Only a resolution produces anything: `notify_for_event` treats `CLOSED` and
    # `OPEN` as silent, so the two other callers of this function get an empty list back
    # and their `enqueue_delivery` is a no-op. Calling it unconditionally rather than
    # behind `if target is RESOLVED` is what keeps the notification a property of the
    # transition rather than of one of the transitions.
    return await notification_service.notify_for_event(session, context, ticket, event)


def record_event(
    session: AsyncSession,
    context: TenantContext,
    ticket: Ticket,
    event_type: TicketEventType,
    *,
    from_value: str | None = None,
    to_value: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> TicketEvent:
    """Append to the ticket's timeline. Never commits — the caller is mid-transaction.

    Public because the message and attachment services write timeline entries too, and
    one event writer is the point: every entry then carries the authenticated caller as
    its actor and the ticket's own organization, rather than each caller assembling
    those itself and one of them eventually getting it wrong.

    `from_value`/`to_value` are `String(100)` and describe a field this event *changed*.
    An event that carries something else — an attachment's filename, say — belongs in
    `extra_data`, which is JSONB and exists for exactly that; a name squeezed into
    `to_value` would be truncated at 100 characters and would put text in a column the
    renderer reads as "the value this changed to".
    """
    event = TicketEvent(
        organization_id=context.organization_id,
        ticket_id=ticket.id,
        event_type=event_type,
        # The authenticated caller, not a request field. An event naming its own actor
        # would be a timeline that can lie about who did what.
        actor_user_id=context.user_id,
        from_value=from_value,
        to_value=to_value,
        # Always a dict: the column is `nullable=False` over `'{}'::jsonb`.
        extra_data=extra_data or {},
    )
    return TicketEventRepository(session, context).add(event)


# ---------------------------------------------------------------------------
# Shared lookups
# ---------------------------------------------------------------------------


async def require_visible_ticket(
    session: AsyncSession, context: TenantContext, ticket_id: uuid.UUID
) -> Ticket:
    """The ticket, or 404.

    Public because the message service needs the identical resolution before it touches
    a thread: a message is reachable exactly when its ticket is, so there is one
    implementation of that rule and it lives here rather than in each caller.
    """
    ticket = await TicketRepository(session, context).get_visible(ticket_id)
    if ticket is None:
        raise NotFoundError(ErrorCode.TICKET_NOT_FOUND)
    return ticket


async def _customer_is_visible(
    session: AsyncSession, context: TenantContext, customer_id: uuid.UUID
) -> bool:
    """Whether a customer id names a customer in the caller's own organization."""
    return await CustomerRepository(session, context).get(customer_id) is not None


async def _agent_is_visible(
    session: AsyncSession, context: TenantContext, agent_id: uuid.UUID
) -> bool:
    """Whether a user id names a user in the caller's own organization.

    The same tenant filter that makes a cross-tenant read a 404 applies here, so
    assigning a ticket to an agent in another organization is not merely refused — it
    is indistinguishable from assigning it to an id that does not exist.
    """
    return await UserRepository(session, context).get(agent_id) is not None
