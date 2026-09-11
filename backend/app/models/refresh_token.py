"""Refresh token — revocable session credential.

Not in the spec's model list, but required by its demand for revocable refresh
tokens (§10). See ADR-003 for why these are opaque and stored rather than JWTs.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(UUIDPrimaryKeyMixin, OrganizationScopedMixin, Base):
    """A single issued refresh token.

    Rotation: each use issues a new token and marks the old one used, linking the
    new one via `replaced_by_id`. Presenting an already-used token signals theft,
    so the entire chain is revoked rather than just the replayed token.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        # Covers the hot path: find a live token for this user.
        Index("ix_refresh_tokens_user_id_revoked_at", "user_id", "revoked_at"),
        # Supports the periodic sweep of expired rows.
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SHA-256 of the token, never the token itself: a database read must not yield
    # usable credentials. Unique so a hash collision cannot authenticate twice.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Set on logout, on rotation, or when a reuse is detected. NULL means live.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Self-reference forming the rotation chain, used to revoke a whole family.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"),
    )

    # Recorded to make suspicious reuse investigable. Not used for authorization:
    # both values are client-controlled and trivially spoofed.
    user_agent: Mapped[str | None] = mapped_column(String(500))
    ip_address: Mapped[str | None] = mapped_column(String(45))  # fits IPv6

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")

    def __repr__(self) -> str:
        state = "revoked" if self.revoked_at else "active"
        return f"<RefreshToken {self.id} ({state})>"
