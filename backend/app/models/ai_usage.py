"""AIUsage — per-call token and cost ledger."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, UUIDPrimaryKeyMixin
from app.models.enums import AI_OPERATION_ENUM, AIOperation

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.ticket import Ticket
    from app.models.user import User


class AIUsage(UUIDPrimaryKeyMixin, OrganizationScopedMixin, Base):
    """One provider call, recorded for cost attribution and rate limiting.

    Separate from `AIAnalysis` on purpose. That table holds what the model said and
    is scoped to a ticket; this one holds what the call cost and exists for every
    AI call including embeddings, which have no ticket. Aggregating spend from
    analyses alone would silently omit ingestion cost.

    Append-only. Cost is computed at write time from the provider's published rate,
    because rates change and a historical row must keep the price actually charged.
    """

    __tablename__ = "ai_usage"
    # All three indexes below lead with organization_id.
    __org_index__ = False
    __table_args__ = (
        # Monthly spend rollup per tenant, and quota enforcement.
        Index("ix_ai_usage_org_created", "organization_id", "created_at"),
        # Cost broken down by operation — which feature is expensive.
        Index("ix_ai_usage_org_operation_created", "organization_id", "operation", "created_at"),
        # Per-user attribution for abuse investigation.
        Index("ix_ai_usage_org_user_created", "organization_id", "user_id", "created_at"),
        CheckConstraint("prompt_tokens >= 0", name="prompt_tokens_non_negative"),
        CheckConstraint("completion_tokens >= 0", name="completion_tokens_non_negative"),
        CheckConstraint("cost_usd >= 0", name="cost_non_negative"),
    )

    operation: Mapped[AIOperation] = mapped_column(AI_OPERATION_ENUM, nullable=False)

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # Numeric, not Float: this is money and gets summed over many rows, where
    # binary floating-point error accumulates. Six decimal places because
    # per-token prices are quoted in fractions of a cent.
    cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, default=0, server_default=text("0")
    )

    # Both nullable: embedding calls during document ingestion belong to no ticket
    # and no interactive user. SET NULL so cost history survives their deletion —
    # spend that vanishes when a ticket is deleted cannot be reconciled.
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="SET NULL"),
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    latency_ms: Mapped[int | None] = mapped_column(Integer)

    # A failed call still consumed quota and may still have been billed, so it is
    # recorded rather than dropped.
    was_successful: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # True when the result came from cache and no provider call was made. Lets the
    # cache's actual saving be measured instead of estimated.
    was_cached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    organization: Mapped["Organization"] = relationship()
    ticket: Mapped["Ticket | None"] = relationship()
    user: Mapped["User | None"] = relationship()

    def __repr__(self) -> str:
        return f"<AIUsage {self.operation} {self.model} ${self.cost_usd}>"
