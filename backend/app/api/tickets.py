"""Ticket endpoints.

Three routes here change one field each — assignment, priority, and status — instead of
a single `PATCH /tickets/{id}`. That is spec §5's requirement that "status is never
mutated by a blind field update" applied consistently: a ticket's status, its priority,
and its assignee each have their own capability in `docs/requirements.md` §3, so each
gets its own endpoint and its own guard. One `PATCH` accepting all three would have to
check three capabilities and validate a lifecycle edge, and would make "which capability
did this request need?" depend on its body rather than on where it was sent.

The list endpoint is where row scope becomes visible to a client: the same
`GET /tickets` returns the whole organization's queue to an admin, one agent's queue to
that agent, and one customer's own tickets to that customer. None of them can ask for a
different one.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import Context, DbSession, Origin, require_permission
from app.core.exceptions import ValidationError
from app.core.permissions import Permission
from app.models.enums import TicketPriority, TicketStatus
from app.models.ticket import Ticket
from app.schemas.fields import SortOrder
from app.schemas.ticket import (
    TicketAssign,
    TicketCreate,
    TicketEventRead,
    TicketPriorityUpdate,
    TicketRead,
    TicketSortKey,
    TicketStatusUpdate,
)
from app.services import sla_service, ticket_service

router = APIRouter()


async def _read(db: DbSession, context: Context, ticket: Ticket) -> TicketRead:
    """One ticket as a response, with its SLA position attached. **Every route uses this.**

    Decoration happens at the route rather than inside `ticket_service`, so the service
    does not grow a dependency on the SLA module for a field only its routes want, and the
    `SLA_VIEW` check sits where the `TenantContext` already is.

    **All eight ticket-returning routes use it, not just the two that read.** The first
    draft decorated `GET /tickets` and `GET /tickets/{id}` only, on the reasoning that the
    mutation routes echo the ticket back rather than reporting it. The flaw is that
    `TicketRead.sla` has a default of `None`, so an undecorated response does not omit the
    field — it serializes `"sla": null`, which is the same payload a customer gets.
    A client that does the obvious thing, `setTicket(await assign(...))`, would therefore
    watch the countdown disappear off the screen the moment somebody assigned the ticket,
    and have no way to tell that from the authorization case. One helper makes "a ticket
    response carries its clock" a property of the module rather than of eight call sites.

    The list route keeps its own version, because it batches: one pair of queries for the
    page rather than one per ticket (`sla_service.decorate`).
    """
    sla = await sla_service.decorate(db, context, [ticket])
    return TicketRead.model_validate(ticket).with_sla(sla.get(ticket.id))


@router.get(
    "",
    response_model=list[TicketRead],
    summary="List, filter, search, and sort the caller's tickets",
    dependencies=[Depends(require_permission(Permission.TICKET_LIST))],
)
async def list_tickets(
    context: Context,
    db: DbSession,
    status_filter: Annotated[TicketStatus | None, Query(alias="status")] = None,
    priority: Annotated[TicketPriority | None, Query()] = None,
    assigned_agent_id: Annotated[uuid.UUID | None, Query()] = None,
    unassigned: Annotated[bool, Query(description="Only tickets with nobody assigned.")] = False,
    customer_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[
        str | None,
        Query(
            max_length=200,
            description="Match subject, description, message content, number, or customer",
        ),
    ] = None,
    created_after: Annotated[datetime | None, Query(description="Inclusive lower bound.")] = None,
    created_before: Annotated[datetime | None, Query(description="Exclusive upper bound.")] = None,
    sort: Annotated[TicketSortKey, Query()] = TicketSortKey.CREATED_AT,
    order: Annotated[SortOrder, Query()] = SortOrder.DESC,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TicketRead]:
    """A page of tickets, narrowed to what the caller may reach.

    **The filters narrow and never widen.** An agent passing `assigned_agent_id` naming a
    colleague gets an empty page, not that colleague's queue, because the row scope is
    applied independently of the query string — a security test asserts exactly that,
    since "a query parameter that grants access" is the classic form of this bug.

    `q` searches spec §14's four fields: the subject and description, any message on the
    ticket, the ticket's number, and the customer's name and email. The message arm is
    the one to watch — it is dropped entirely for a caller without
    `MESSAGE_READ_INTERNAL`, so a portal account cannot find a ticket by typing a phrase
    that appears only in an internal note. That is the same rule the thread and the
    attachment list apply, enforced at the same layer.

    `q` typed as a bare number matches the ticket number exactly: `q=1042` finds ticket
    #1042 and not #10420. A `q` that is not a number is not an error — it simply matches
    no number, and the other three arms still apply.

    **The assignee filter is tri-state**, which is why it is two parameters:

    * `assigned_agent_id=<uuid>` — that agent's tickets;
    * `unassigned=true` — the queue with nobody on it;
    * neither — no filter at all;
    * **both** — `422`. Silently letting one win would answer a question the caller did
      not ask, and the wrong answer would look like a queue with tickets missing.

    `created_after` is inclusive and `created_before` exclusive, so adjacent windows tile
    without a row appearing in both. Sorting is by `sort`, and ties are always broken by
    ticket id, so paging through equal timestamps cannot repeat or skip a ticket.
    `sort=priority` orders by the enum's declaration order, so `order=desc` puts `URGENT`
    first.
    """
    if assigned_agent_id is not None and unassigned:
        # `ValidationError` rather than `HTTPException`: it is the app's own 422, with
        # the same error envelope as every other refusal, so a client parses one shape.
        raise ValidationError(
            "Cannot filter by an assigned agent and by unassigned at the same time."
        )

    tickets = await ticket_service.list_tickets(
        db,
        context,
        status=status_filter,
        priority=priority,
        assigned_agent_id=assigned_agent_id,
        unassigned=unassigned,
        customer_id=customer_id,
        term=q,
        created_after=created_after,
        created_before=created_before,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    # One extra pair of queries for the whole page, not per ticket — see
    # `sla_service.decorate`. Empty for a portal caller, which makes `sla` null.
    sla = await sla_service.decorate(db, context, tickets)
    return [TicketRead.model_validate(ticket).with_sla(sla.get(ticket.id)) for ticket in tickets]


@router.post(
    "",
    response_model=TicketRead,
    status_code=201,
    summary="Raise a ticket",
    dependencies=[Depends(require_permission(Permission.TICKET_CREATE))],
)
async def create_ticket(
    payload: TicketCreate, context: Context, db: DbSession, origin: Origin
) -> TicketRead:
    """Raise a ticket.

    Staff name the customer; a portal caller must not, and their own linked customer is
    used instead. A ticket raised by a customer is owned by them from the first
    request — there is no window in which it is unowned.

    `priority` is accepted only from a caller holding `TICKET_CHANGE_PRIORITY`, so a
    customer raising an urgent ticket does not get to decide that it is urgent.
    """
    ticket = await ticket_service.create_ticket(db, context, payload, origin=origin)
    return await _read(db, context, ticket)


@router.get(
    "/{ticket_id}",
    response_model=TicketRead,
    summary="Fetch one ticket",
    dependencies=[Depends(require_permission(Permission.TICKET_VIEW))],
)
async def get_ticket(ticket_id: uuid.UUID, context: Context, db: DbSession) -> TicketRead:
    """One ticket the caller may reach.

    A ticket in another organization and a ticket assigned to a colleague both return
    the identical 404 — an agent should not be able to size the desk's workload by
    watching which ids are refused (ADR-009).
    """
    ticket = await ticket_service.get_ticket(db, context, ticket_id)
    sla = await sla_service.decorate(db, context, [ticket])
    return TicketRead.model_validate(ticket).with_sla(sla.get(ticket.id))


@router.get(
    "/{ticket_id}/events",
    response_model=list[TicketEventRead],
    summary="A ticket's activity timeline",
    dependencies=[Depends(require_permission(Permission.TICKET_VIEW))],
)
async def list_events(
    ticket_id: uuid.UUID, context: Context, db: DbSession
) -> list[TicketEventRead]:
    """Everything that has happened to this ticket, oldest first.

    Unpaginated. A ticket's history is bounded by how much work was done on it, and the
    timeline is read whole — a page limit would hide the beginning of the story rather
    than the end. Guarded by `TICKET_VIEW` because spec §3 has no separate row for
    reading a timeline: it is part of seeing the ticket.

    Internal-note entries appear only for a caller holding `MESSAGE_READ_INTERNAL`, so
    the timeline and the thread agree about who knows a note exists. SLA warning and
    breach entries appear only for a caller holding `SLA_VIEW`, for the matching reason:
    they name a deadline, and a deadline is the policy. A portal caller therefore sees
    neither — the same two capabilities that decide the thread's internal notes and the
    ticket's `sla` object decide this list.
    """
    events = await ticket_service.list_events(db, context, ticket_id)
    return [TicketEventRead.model_validate(event) for event in events]


@router.post(
    "/{ticket_id}/assign",
    response_model=TicketRead,
    summary="Assign, reassign, or unassign a ticket",
    dependencies=[Depends(require_permission(Permission.TICKET_ASSIGN))],
)
async def assign_ticket(
    ticket_id: uuid.UUID, payload: TicketAssign, context: Context, db: DbSession, origin: Origin
) -> TicketRead:
    """Assign a ticket to an agent, or clear the assignment with `null`.

    Assigning an `OPEN` ticket also moves it to `ASSIGNED` — that is the one lifecycle
    edge assignment owns, and doing it here is what makes "status is `ASSIGNED`, nobody
    is assigned" unreachable. Unassigning a ticket whose status is `ASSIGNED` is
    refused for the same reason, from the other direction.
    """
    ticket = await ticket_service.assign_ticket(
        db,
        context,
        ticket_id,
        assigned_agent_id=payload.assigned_agent_id,
        origin=origin,
    )
    return await _read(db, context, ticket)


@router.post(
    "/{ticket_id}/priority",
    response_model=TicketRead,
    summary="Change a ticket's priority",
    dependencies=[Depends(require_permission(Permission.TICKET_CHANGE_PRIORITY))],
)
async def change_priority(
    ticket_id: uuid.UUID,
    payload: TicketPriorityUpdate,
    context: Context,
    db: DbSession,
    origin: Origin,
) -> TicketRead:
    """Set the effective business priority.

    `ai_recommended_priority` is left untouched, so an override never destroys the
    recommendation it overrode — that separation is what makes model accuracy
    measurable over time (spec §6).
    """
    ticket = await ticket_service.change_priority(db, context, ticket_id, payload, origin=origin)
    return await _read(db, context, ticket)


@router.post(
    "/{ticket_id}/status",
    response_model=TicketRead,
    summary="Move a ticket between working states",
    dependencies=[Depends(require_permission(Permission.TICKET_CHANGE_STATUS))],
)
async def change_status(
    ticket_id: uuid.UUID,
    payload: TicketStatusUpdate,
    context: Context,
    db: DbSession,
    origin: Origin,
) -> TicketRead:
    """Move a ticket along one edge of the lifecycle.

    Performs the four edges between working states. Assignment, closing, and reopening
    are refused here with a pointer to the endpoint that owns them, so the capability a
    request needs is always a property of where it was sent.
    """
    ticket = await ticket_service.change_status(db, context, ticket_id, payload, origin=origin)
    return await _read(db, context, ticket)


@router.post(
    "/{ticket_id}/close",
    response_model=TicketRead,
    summary="Close a resolved ticket",
    dependencies=[Depends(require_permission(Permission.TICKET_CLOSE))],
)
async def close_ticket(
    ticket_id: uuid.UUID, context: Context, db: DbSession, origin: Origin
) -> TicketRead:
    """Confirm a resolution and close the ticket.

    Only `RESOLVED → CLOSED`. This is the capability a customer holds — agreeing that
    the fix worked — which is a different act from an agent deciding it is resolved.
    Closing an `IN_PROGRESS` ticket is refused with `409`, rather than walking it
    through two transitions that would skip the confirmation entirely.
    """
    ticket = await ticket_service.close_ticket(db, context, ticket_id, origin=origin)
    return await _read(db, context, ticket)


@router.post(
    "/{ticket_id}/reopen",
    response_model=TicketRead,
    summary="Reopen a closed ticket",
    dependencies=[Depends(require_permission(Permission.TICKET_REOPEN))],
)
async def reopen_ticket(
    ticket_id: uuid.UUID, context: Context, db: DbSession, origin: Origin
) -> TicketRead:
    """Reopen a closed ticket. `CLOSED → OPEN`, an explicit action only.

    The terminal timestamps and the assignment are cleared, so `OPEN` keeps its meaning
    of "nobody owns this yet" and the ticket re-enters the lifecycle at the beginning.
    The history survives in the ticket's event timeline, which is where it belongs —
    `ticket_events` still shows who worked it, not just the fields they left behind.
    """
    ticket = await ticket_service.reopen_ticket(db, context, ticket_id, origin=origin)
    return await _read(db, context, ticket)
