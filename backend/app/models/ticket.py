"""Ticket — the central domain entity."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    SENTIMENT_ENUM,
    TICKET_PRIORITY_ENUM,
    TICKET_STATUS_ENUM,
    Sentiment,
    TicketPriority,
    TicketStatus,
)

if TYPE_CHECKING:
    from app.models.ai_analysis import AIAnalysis
    from app.models.attachment import Attachment
    from app.models.customer import Customer
    from app.models.message import Message
    from app.models.organization import Organization
    from app.models.ticket_event import TicketEvent
    from app.models.user import User


class Ticket(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A support request.

    Two priority fields exist deliberately: `priority` is the effective business
    value, `ai_recommended_priority` is what the model suggested. Keeping them
    separate lets business rules override AI without destroying the recommendation,
    and makes model accuracy measurable over time (spec §13).
    """

    __tablename__ = "tickets"
    # Every index below leads with organization_id, so the mixin's standalone index
    # would duplicate work on each insert without serving any query these do not.
    __org_index__ = False
    __table_args__ = (
        # --- Composite indexes -------------------------------------------------
        # Column order is deliberate: organization_id leads every index because
        # every query filters on it first. A single-column index on status would
        # be near-useless here, since status has six values and would match a
        # large share of a tenant's rows.
        #
        # Agent queue: "my open tickets, newest first".
        Index(
            "ix_tickets_org_agent_status_created",
            "organization_id",
            "assigned_agent_id",
            "status",
            "created_at",
        ),
        # Manager queue: "all open tickets by priority".
        Index(
            "ix_tickets_org_status_priority",
            "organization_id",
            "status",
            "priority",
        ),
        # Default list ordering and date-range filters.
        Index("ix_tickets_org_created_at", "organization_id", "created_at"),
        # Customer portal: "my tickets".
        Index("ix_tickets_org_customer_created", "organization_id", "customer_id", "created_at"),
        # Analytics grouping by category.
        Index("ix_tickets_org_category", "organization_id", "category"),
        # SLA sweep: scans unresolved tickets only, so a partial index keeps it
        # small and excludes the resolved/closed majority of a mature dataset.
        Index(
            "ix_tickets_sla_pending",
            "organization_id",
            "priority",
            "created_at",
            postgresql_where=text("status NOT IN ('resolved', 'closed')"),
        ),
        # Full-text search across subject and description. The expression must
        # match the query exactly for the index to be used.
        Index(
            "ix_tickets_fts",
            text("to_tsvector('english', subject || ' ' || description)"),
            postgresql_using="gin",
        ),
        # --- Invariants --------------------------------------------------------
        # A per-tenant sequential number for human reference ("ticket #1042").
        # Unique per organization, so tenant A's #1 is distinct from tenant B's.
        Index("uq_tickets_org_number", "organization_id", "number", unique=True),
        # Confidence scores are probabilities; anything outside [0,1] is a bug.
        CheckConstraint(
            "ai_classification_confidence IS NULL "
            "OR (ai_classification_confidence >= 0 AND ai_classification_confidence <= 1)",
            name="ai_classification_confidence_range",
        ),
        CheckConstraint(
            "sentiment_confidence IS NULL "
            "OR (sentiment_confidence >= 0 AND sentiment_confidence <= 1)",
            name="sentiment_confidence_range",
        ),
        # Terminal timestamps must agree with terminal states.
        CheckConstraint(
            "(status = 'resolved' AND resolved_at IS NOT NULL) "
            "OR (status = 'closed' AND resolved_at IS NOT NULL) "
            "OR (status NOT IN ('resolved', 'closed'))",
            name="resolved_at_matches_status",
        ),
    )

    # Human-facing reference, assigned per organization.
    #
    # No sequence backs this: a global sequence would leak total volume across
    # tenants and skip numbers per tenant. The service layer allocates it, so two
    # concurrent inserts can pick the same value — the unique index below is what
    # makes that a retryable integrity error rather than a duplicate.
    number: Mapped[int] = mapped_column(Integer, nullable=False)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )

    # SET NULL on delete: deactivating an agent must not delete their tickets.
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[TicketStatus] = mapped_column(
        TICKET_STATUS_ENUM,
        nullable=False,
        default=TicketStatus.OPEN,
        server_default=TicketStatus.OPEN.value,
    )

    # Effective business priority. May exceed the AI recommendation.
    priority: Mapped[TicketPriority] = mapped_column(
        TICKET_PRIORITY_ENUM,
        nullable=False,
        default=TicketPriority.MEDIUM,
        server_default=TicketPriority.MEDIUM.value,
    )

    category: Mapped[str | None] = mapped_column(String(100))
    subcategory: Mapped[str | None] = mapped_column(String(100))

    # --- AI-derived fields ---------------------------------------------------
    # Nullable throughout: analysis is asynchronous, so a ticket is fully usable
    # before any of these are populated, and stays usable if the provider fails.
    sentiment: Mapped[Sentiment | None] = mapped_column(SENTIMENT_ENUM)
    sentiment_confidence: Mapped[float | None] = mapped_column(Float)

    # Same type object as `priority` above, so only one CREATE TYPE is emitted.
    ai_recommended_priority: Mapped[TicketPriority | None] = mapped_column(TICKET_PRIORITY_ENUM)
    ai_priority_score: Mapped[float | None] = mapped_column(Float)
    ai_classification_confidence: Mapped[float | None] = mapped_column(Float)

    # --- Lifecycle timestamps -----------------------------------------------
    # Recorded separately from status so SLA math and analytics can measure
    # elapsed time without reconstructing history from the event log.
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped["Organization"] = relationship(back_populates="tickets")
    customer: Mapped["Customer"] = relationship(back_populates="tickets")
    assigned_agent: Mapped["User | None"] = relationship()

    # Ordered by insertion so a conversation reads chronologically without an
    # explicit ORDER BY at every call site.
    messages: Mapped[list["Message"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )
    events: Mapped[list["TicketEvent"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketEvent.created_at",
    )
    ai_analyses: Mapped[list["AIAnalysis"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="AIAnalysis.created_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<Ticket #{self.number} {self.status}>"
