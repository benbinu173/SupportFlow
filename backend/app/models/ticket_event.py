"""TicketEvent — an entry in a ticket's activity timeline."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, UUIDPrimaryKeyMixin
from app.models.enums import TICKET_EVENT_TYPE_ENUM, TicketEventType

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.ticket import Ticket
    from app.models.user import User


class TicketEvent(UUIDPrimaryKeyMixin, OrganizationScopedMixin, Base):
    """A single thing that happened to a ticket.

    Distinct from `AuditLog`: this is the user-facing timeline shown on the ticket
    detail screen, so it records what a human needs to read. AuditLog answers
    compliance questions across the whole organization. Separating them keeps the
    timeline query cheap and lets the two evolve independently.

    Append-only, hence no `updated_at`. Editing history would defeat the point.
    """

    __tablename__ = "ticket_events"
    __table_args__ = (
        # The only read pattern: one ticket's timeline in order.
        Index("ix_ticket_events_ticket_created", "ticket_id", "created_at"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )

    event_type: Mapped[TicketEventType] = mapped_column(TICKET_EVENT_TYPE_ENUM, nullable=False)

    # NULL when the system acted rather than a person — SLA breaches and completed
    # AI analyses have no actor.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    # Before/after values for the field this event changed. Text rather than typed
    # columns because a single event type has to describe status, priority, and
    # assignee changes alike. Rendered for display only, never parsed back.
    from_value: Mapped[str | None] = mapped_column(String(100))
    to_value: Mapped[str | None] = mapped_column(String(100))

    # Anything else the specific event type needs to render. Deliberately loose:
    # a timeline entry's shape varies by type and this table must not gain a
    # column every time a new event is added.
    #
    # `none_as_null` so an explicit None fails the NOT NULL check instead of
    # storing JSON `null`, which would pass it.
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    organization: Mapped["Organization"] = relationship()
    ticket: Mapped["Ticket"] = relationship(back_populates="events")
    actor: Mapped["User | None"] = relationship()

    def __repr__(self) -> str:
        return f"<TicketEvent {self.event_type} on {self.ticket_id}>"
