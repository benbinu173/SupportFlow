"""AIAnalysis — the outcome of one AI operation on a ticket."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    AI_OPERATION_ENUM,
    PROCESSING_STATUS_ENUM,
    AIOperation,
    ProcessingStatus,
)

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.ticket import Ticket


class AIAnalysis(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One AI call and its result.

    Kept as history rather than overwritten, which is what makes the AI auditable:
    which model produced a classification, when, with what confidence, and what it
    cost. The spec requires this (§13, §33) and it is also what lets a regression in
    a provider's model be spotted after the fact.

    A row exists from the moment work is queued, so a pending or failed analysis is
    visible rather than silently absent.
    """

    __tablename__ = "ai_analyses"
    # Both indexes below lead with organization_id.
    __org_index__ = False
    __table_args__ = (
        # Latest analysis for a ticket, per operation — the ticket detail screen
        # reads exactly this.
        Index("ix_ai_analyses_ticket_operation_created", "ticket_id", "operation", "created_at"),
        # Retry sweep and queue monitoring; partial, because completed rows
        # dominate the table and are never scanned this way.
        Index(
            "ix_ai_analyses_pending",
            "organization_id",
            "created_at",
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
        # A terminal state must explain itself: completed analyses carry a result,
        # failed ones carry an error. Without this, a silently empty completed row
        # is indistinguishable from a successful one.
        #
        # JSON `null` is excluded explicitly. SQL NULL and JSONB 'null' are
        # different values, and only the former fails IS NOT NULL — so a raw INSERT
        # could otherwise mark a row completed with no usable payload.
        CheckConstraint(
            "(status = 'completed' AND result IS NOT NULL AND result <> 'null'::jsonb) "
            "OR (status = 'failed' AND error_message IS NOT NULL) "
            "OR status IN ('pending', 'processing')",
            name="terminal_status_has_payload",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="latency_non_negative",
        ),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )

    operation: Mapped[AIOperation] = mapped_column(AI_OPERATION_ENUM, nullable=False)

    status: Mapped[ProcessingStatus] = mapped_column(
        PROCESSING_STATUS_ENUM,
        nullable=False,
        default=ProcessingStatus.PENDING,
        server_default=ProcessingStatus.PENDING.value,
    )

    # The parsed, validated model output. JSONB because each operation returns a
    # different shape, and because the whole payload is worth keeping — reducing it
    # to the few fields copied onto `tickets` would discard the evidence.
    #
    # `none_as_null` is not the default: SQLAlchemy otherwise persists Python None
    # as JSON `null`, which is a present value in SQL and satisfies IS NOT NULL.
    # A completed-but-empty analysis would then pass the constraint below, and read
    # back as None through the ORM — invisible from Python either way.
    #
    # Treated as untrusted input (spec §4): validated before anything reads it, and
    # never rendered as markup.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))

    confidence: Mapped[float | None] = mapped_column(Float)

    # --- Provider attribution ------------------------------------------------
    # Recorded per row, not read from config at display time: config changes, and a
    # historical analysis must still say which model actually produced it.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    # Provider-side message for a failure. Surfaced to staff, never to customers:
    # upstream errors can echo prompt content.
    error_message: Mapped[str | None] = mapped_column(Text)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped["Organization"] = relationship()
    ticket: Mapped["Ticket"] = relationship(back_populates="ai_analyses")

    def __repr__(self) -> str:
        return f"<AIAnalysis {self.operation} {self.status}>"
