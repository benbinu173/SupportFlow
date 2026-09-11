"""User — staff and customer-portal accounts."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import USER_ROLE_ENUM, UserRole

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.organization import Organization
    from app.models.refresh_token import RefreshToken


class User(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """An authenticated principal.

    Covers all four roles. A `customer`-role user is additionally linked to a
    `Customer` record, which carries the CRM-side contact details.
    """

    __tablename__ = "users"
    __table_args__ = (
        # Email is unique *per organization*, not globally: the same person may
        # legitimately hold accounts with two different support providers. Making
        # this global would leak the existence of an account in another tenant.
        #
        # Unnamed so the metadata naming convention generates it. An explicit name
        # would be used verbatim, and a unique constraint creates an index — whose
        # name has to be unique across the whole schema, not just this table.
        UniqueConstraint("organization_id", "email"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)

    # Argon2id (ADR-001). Never exposed through any API response.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        USER_ROLE_ENUM,
        nullable=False,
        index=True,
    )

    # Deactivation is preferred over deletion so audit history stays intact.
    # Checked on every authenticated request, not just at login.
    is_active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set only for role=customer. SET NULL rather than CASCADE: removing a CRM
    # contact record should not silently delete the login account.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        index=True,
    )

    organization: Mapped["Organization"] = relationship(back_populates="users")
    customer: Mapped["Customer | None"] = relationship(back_populates="portal_users")
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"
