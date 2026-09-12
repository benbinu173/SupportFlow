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
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import Context, DbSession, require_permission
from app.core.permissions import Permission
from app.models.enums import TicketPriority, TicketStatus
from app.schemas.ticket import (
    TicketAssign,
    TicketCreate,
    TicketEventRead,
    TicketPriorityUpdate,
    TicketRead,
    TicketStatusUpdate,
)
from app.services import ticket_service

router = APIRouter()


@router.get(
    "",
    response_model=list[TicketRead],
    summary="List the caller's tickets",
    dependencies=[Depends(require_permission(Permission.TICKET_LIST))],
)
async def list_tickets(
    context: Context,
    db: DbSession,
    status_filter: Annotated[TicketStatus | None, Query(alias="status")] = None,
    priority: Annotated[TicketPriority | None, Query()] = None,
    assigned_agent_id: Annotated[uuid.UUID | None, Query()] = None,
    customer_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TicketRead]:
    """A page of tickets, newest first, narrowed to what the caller may reach.

    The filters narrow and never widen. An agent passing `assigned_agent_id` naming a
    colleague gets an empty page, not that colleague's queue, because the row scope is
    applied independently of the query string — a test asserts exactly that, since "a
    query parameter that grants access" is the classic form of this bug.

    `assigned_agent_id=null` is not expressible here: an absent parameter means "no
    filter", and there is no way to ask for the unassigned queue. That needs a
    tri-state parameter, and it arrives with the queue filters in Phase N rather than
    being guessed at now.
    """
    tickets = await ticket_service.list_tickets(
        db,
        context,
        status=status_filter,
        priority=priority,
        assigned_agent_id=assigned_agent_id,
        customer_id=customer_id,
        limit=limit,
        offset=offset,
    )
    return [TicketRead.model_validate(ticket) for ticket in tickets]


@router.post(
    "",
    response_model=TicketRead,
    status_code=201,
    summary="Raise a ticket",
    dependencies=[Depends(require_permission(Permission.TICKET_CREATE))],
)
async def create_ticket(payload: TicketCreate, context: Context, db: DbSession) -> TicketRead:
    """Raise a ticket.

    Staff name the customer; a portal caller must not, and their own linked customer is
    used instead. A ticket raised by a customer is owned by them from the first
    request — there is no window in which it is unowned.

    `priority` is accepted only from a caller holding `TICKET_CHANGE_PRIORITY`, so a
    customer raising an urgent ticket does not get to decide that it is urgent.
    """
    ticket = await ticket_service.create_ticket(db, context, payload)
    return TicketRead.model_validate(ticket)


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
    return TicketRead.model_validate(ticket)


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
    the timeline and the thread agree about who knows a note exists.
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
    ticket_id: uuid.UUID, payload: TicketAssign, context: Context, db: DbSession
) -> TicketRead:
    """Assign a ticket to an agent, or clear the assignment with `null`.

    Assigning an `OPEN` ticket also moves it to `ASSIGNED` — that is the one lifecycle
    edge assignment owns, and doing it here is what makes "status is `ASSIGNED`, nobody
    is assigned" unreachable. Unassigning a ticket whose status is `ASSIGNED` is
    refused for the same reason, from the other direction.
    """
    ticket = await ticket_service.assign_ticket(
        db, context, ticket_id, assigned_agent_id=payload.assigned_agent_id
    )
    return TicketRead.model_validate(ticket)


@router.post(
    "/{ticket_id}/priority",
    response_model=TicketRead,
    summary="Change a ticket's priority",
    dependencies=[Depends(require_permission(Permission.TICKET_CHANGE_PRIORITY))],
)
async def change_priority(
    ticket_id: uuid.UUID, payload: TicketPriorityUpdate, context: Context, db: DbSession
) -> TicketRead:
    """Set the effective business priority.

    `ai_recommended_priority` is left untouched, so an override never destroys the
    recommendation it overrode — that separation is what makes model accuracy
    measurable over time (spec §6).
    """
    ticket = await ticket_service.change_priority(db, context, ticket_id, payload)
    return TicketRead.model_validate(ticket)


@router.post(
    "/{ticket_id}/status",
    response_model=TicketRead,
    summary="Move a ticket between working states",
    dependencies=[Depends(require_permission(Permission.TICKET_CHANGE_STATUS))],
)
async def change_status(
    ticket_id: uuid.UUID, payload: TicketStatusUpdate, context: Context, db: DbSession
) -> TicketRead:
    """Move a ticket along one edge of the lifecycle.

    Performs the four edges between working states. Assignment, closing, and reopening
    are refused here with a pointer to the endpoint that owns them, so the capability a
    request needs is always a property of where it was sent.
    """
    ticket = await ticket_service.change_status(db, context, ticket_id, payload)
    return TicketRead.model_validate(ticket)


@router.post(
    "/{ticket_id}/close",
    response_model=TicketRead,
    summary="Close a resolved ticket",
    dependencies=[Depends(require_permission(Permission.TICKET_CLOSE))],
)
async def close_ticket(ticket_id: uuid.UUID, context: Context, db: DbSession) -> TicketRead:
    """Confirm a resolution and close the ticket.

    Only `RESOLVED → CLOSED`. This is the capability a customer holds — agreeing that
    the fix worked — which is a different act from an agent deciding it is resolved.
    Closing an `IN_PROGRESS` ticket is refused with `409`, rather than walking it
    through two transitions that would skip the confirmation entirely.
    """
    ticket = await ticket_service.close_ticket(db, context, ticket_id)
    return TicketRead.model_validate(ticket)


@router.post(
    "/{ticket_id}/reopen",
    response_model=TicketRead,
    summary="Reopen a closed ticket",
    dependencies=[Depends(require_permission(Permission.TICKET_REOPEN))],
)
async def reopen_ticket(ticket_id: uuid.UUID, context: Context, db: DbSession) -> TicketRead:
    """Reopen a closed ticket. `CLOSED → OPEN`, an explicit action only.

    The terminal timestamps and the assignment are cleared, so `OPEN` keeps its meaning
    of "nobody owns this yet" and the ticket re-enters the lifecycle at the beginning.
    The history survives in the ticket's event timeline, which is where it belongs —
    `ticket_events` still shows who worked it, not just the fields they left behind.
    """
    ticket = await ticket_service.reopen_ticket(db, context, ticket_id)
    return TicketRead.model_validate(ticket)
