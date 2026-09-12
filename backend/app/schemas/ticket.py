"""Ticket request and response schemas.

Note what is *not* here: there is no `TicketUpdate`. Spec §5 requires that status is
never mutated by a blind field update and that transitions go through an action, and
the same reasoning covers priority and assignment — each has its own endpoint and its
own capability (ADR-017). One `PATCH` accepting `status`, `priority`, and
`assigned_agent_id` together would have to check three capabilities and validate an
edge, and would make "which capability did this request need?" depend on its body.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Sentiment, TicketEventType, TicketPriority, TicketStatus


class TicketRead(BaseModel):
    """A ticket as the API presents one."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: int
    customer_id: uuid.UUID
    assigned_agent_id: uuid.UUID | None
    subject: str
    description: str
    status: TicketStatus
    priority: TicketPriority
    category: str | None
    subcategory: str | None

    # AI-derived fields are present from this phase onward even though nothing
    # populates them yet (Phases T-W do). They are nullable throughout, so a client can
    # be written against the final shape now rather than being changed later — and
    # `ai_recommended_priority` sitting beside `priority` is the visible statement that
    # the two are different things.
    sentiment: Sentiment | None
    sentiment_confidence: float | None
    ai_recommended_priority: TicketPriority | None
    ai_classification_confidence: float | None

    first_response_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TicketCreate(BaseModel):
    """Raising a ticket.

    `customer_id` is **required from staff and forbidden from a portal caller** — the
    service decides which, based on the caller's own scope, and a customer who sends
    someone else's id is refused rather than quietly overridden. A request field that
    is ignored when inconvenient is worse than one that is rejected, because the
    caller never learns their input did not mean what they thought.

    `priority` is likewise conditional: it is accepted only from a caller holding
    `TICKET_CHANGE_PRIORITY`. A customer raising an urgent ticket does not get to
    decide it is urgent; that judgement belongs to the queue's owner, and the field is
    refused rather than downgraded for the same reason.
    """

    subject: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=20_000)
    customer_id: uuid.UUID | None = None
    category: str | None = Field(default=None, max_length=100)
    priority: TicketPriority | None = None


class TicketAssign(BaseModel):
    """Assigning or reassigning a ticket.

    Nullable so that unassignment is expressible: `{"assigned_agent_id": null}` returns
    the ticket to the unassigned queue. Making it required would mean the only way to
    unassign was to reassign to someone else.
    """

    assigned_agent_id: uuid.UUID | None = None


class TicketPriorityUpdate(BaseModel):
    """Changing the effective business priority."""

    priority: TicketPriority


class TicketStatusUpdate(BaseModel):
    """Moving a ticket along one edge of the lifecycle.

    The target only — the current status is read from the row, never supplied by the
    client, so a request cannot assert a starting point that would make an illegal
    edge look legal.
    """

    status: TicketStatus


class TicketEventRead(BaseModel):
    """One entry in a ticket's activity timeline."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: TicketEventType
    actor_user_id: uuid.UUID | None
    from_value: str | None
    to_value: str | None
    extra_data: dict[str, Any]
    created_at: datetime
