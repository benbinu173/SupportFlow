"""SLAPolicy — per-priority response and resolution targets."""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Integer, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import TICKET_PRIORITY_ENUM, TicketPriority

if TYPE_CHECKING:
    from app.models.organization import Organization


class SLAPolicy(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """The response and resolution targets for one priority level.

    One row per priority per organization, so a tenant configures four. Stored
    per-tenant rather than as global constants because SLA commitments are a
    business agreement that differs by customer contract.

    Targets are in minutes: the unit is fine enough for an urgent-priority response
    target and integer arithmetic avoids the rounding surprises of storing hours as
    a float.
    """

    __tablename__ = "sla_policies"
    __table_args__ = (
        # One policy per priority per tenant. This is what lets the SLA calculator
        # look up a single row rather than resolving between competing policies.
        UniqueConstraint("organization_id", "priority"),
        CheckConstraint("response_time_minutes > 0", name="response_time_positive"),
        CheckConstraint("resolution_time_minutes > 0", name="resolution_time_positive"),
        # A resolution target that lands before the response target is
        # unsatisfiable — the ticket would breach resolution while still on time
        # for a first reply.
        CheckConstraint(
            "resolution_time_minutes >= response_time_minutes",
            name="resolution_after_response",
        ),
        CheckConstraint(
            "warning_threshold_percent > 0 AND warning_threshold_percent < 100",
            name="warning_threshold_range",
        ),
    )

    priority: Mapped[TicketPriority] = mapped_column(TICKET_PRIORITY_ENUM, nullable=False)

    # Time to first agent reply.
    response_time_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Time to a resolved status.
    resolution_time_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Percentage of the target elapsed before a warning fires, so agents are
    # notified while there is still time to act rather than at the breach.
    warning_threshold_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=80, server_default=text("80")
    )

    # Lets a tenant switch off enforcement for a priority without deleting the
    # configured targets and losing them.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    organization: Mapped["Organization"] = relationship(back_populates="sla_policies")

    def __repr__(self) -> str:
        return f"<SLAPolicy {self.priority}: {self.response_time_minutes}m to respond>"
