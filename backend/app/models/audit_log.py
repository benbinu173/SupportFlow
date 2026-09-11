"""AuditLog — an append-only record of security-relevant actions."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, UUIDPrimaryKeyMixin
from app.models.enums import AUDIT_ACTION_ENUM, AuditAction

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User


class AuditLog(UUIDPrimaryKeyMixin, OrganizationScopedMixin, Base):
    """One auditable action, organization-wide.

    Append-only by design: no `updated_at`, and nothing in the application updates
    or deletes these rows. An audit trail that can be edited answers no compliance
    question. Retention is handled by a scheduled purge, not by in-place edits.

    Targets are recorded as a polymorphic (type, id) pair rather than a foreign key.
    A real FK would either cascade — destroying the record of a deletion, which is
    exactly the event most worth keeping — or block the delete outright.
    """

    __tablename__ = "audit_logs"
    # All four indexes below lead with organization_id.
    __org_index__ = False
    __table_args__ = (
        # Primary review query: an organization's recent activity.
        Index("ix_audit_logs_org_created", "organization_id", "created_at"),
        # "What did this user do?" — an access review.
        Index("ix_audit_logs_org_actor_created", "organization_id", "actor_user_id", "created_at"),
        # "What happened to this record?" — the history of one entity.
        Index("ix_audit_logs_target", "organization_id", "target_type", "target_id"),
        # Filter by action type across a tenant.
        Index("ix_audit_logs_org_action_created", "organization_id", "action", "created_at"),
    )

    action: Mapped[AuditAction] = mapped_column(AUDIT_ACTION_ENUM, nullable=False)

    # NULL for system-initiated actions. SET NULL on user deletion: the log entry
    # must survive the actor being removed, which is the point of an audit trail.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    # Denormalized copy of the actor's email at the time of the action. Kept
    # because `actor_user_id` may become NULL, and "who did this" then has no
    # answer at all. A later email change must not rewrite history either.
    actor_email: Mapped[str | None] = mapped_column(String(320))

    # Polymorphic target: the table name and row id the action applied to.
    # Intentionally not a foreign key — see the class docstring.
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Action-specific detail: changed fields, before/after values, request context.
    # Must never contain credentials, tokens, or password hashes — the audit log is
    # readable by admins and is not a place to widen secret exposure.
    #
    # `none_as_null` so an explicit None fails the NOT NULL check instead of
    # storing JSON `null`, which would pass it.
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # Request provenance. Recorded for investigation, never used for authorization:
    # both are client-controlled.
    ip_address: Mapped[str | None] = mapped_column(String(45))  # fits IPv6
    user_agent: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    organization: Mapped["Organization"] = relationship()
    actor: Mapped["User | None"] = relationship()

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} by {self.actor_email or 'system'}>"
