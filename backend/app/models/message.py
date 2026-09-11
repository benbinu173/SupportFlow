"""Message — one entry in a ticket conversation."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import SENDER_TYPE_ENUM, SenderType

if TYPE_CHECKING:
    from app.models.attachment import Attachment
    from app.models.organization import Organization
    from app.models.ticket import Ticket
    from app.models.user import User


class Message(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A message on a ticket.

    Covers three audiences in one table, discriminated by `sender_type` and
    `is_internal`: customer-visible correspondence, agent-only internal notes, and
    unsent AI drafts. Keeping them together means the timeline is one ordered query;
    keeping them flagged means visibility is a filter the repository always applies.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # The dominant read: a ticket's thread in order.
        Index("ix_messages_ticket_created", "ticket_id", "created_at"),
        # Customer-facing thread. Partial, because the customer portal never reads
        # internal notes and this keeps those rows out of the index entirely.
        Index(
            "ix_messages_ticket_public_created",
            "ticket_id",
            "created_at",
            postgresql_where=text("is_internal = false"),
        ),
        # An internal note is staff-only by definition, so it can never be authored
        # by a customer. Enforced here because the consequence of getting it wrong
        # is leaking private commentary to the customer portal.
        CheckConstraint(
            "NOT (is_internal AND sender_type = 'customer')",
            name="internal_note_not_from_customer",
        ),
        # An AI draft is never customer-visible until an agent sends it, at which
        # point it is re-authored as an agent message.
        CheckConstraint(
            "sender_type <> 'ai_draft' OR is_internal",
            name="ai_draft_is_internal",
        ),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )

    sender_type: Mapped[SenderType] = mapped_column(SENDER_TYPE_ENUM, nullable=False)

    # NULL for system entries and AI drafts, which have no human author. Set for
    # customer and agent messages; SET NULL so deactivating a user preserves the
    # thread rather than deleting history.
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Internal notes are visible to staff only. Defaulted false so a missing value
    # fails safe toward the more restrictive reading — a note wrongly marked public
    # leaks, one wrongly marked internal merely hides.
    is_internal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    organization: Mapped["Organization"] = relationship()
    ticket: Mapped["Ticket"] = relationship(back_populates="messages")
    sender: Mapped["User | None"] = relationship()
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="message")

    def __repr__(self) -> str:
        scope = "internal" if self.is_internal else "public"
        return f"<Message {self.id} {self.sender_type} ({scope})>"
