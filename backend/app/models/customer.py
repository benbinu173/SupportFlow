"""Customer — an end user who raises tickets."""

from typing import TYPE_CHECKING, Any

from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.ticket import Ticket
    from app.models.user import User


class Customer(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A customer contact record.

    Distinct from `User`: a customer may exist as a support contact without ever
    holding portal credentials. When they do log in, a `User` row with
    `role=customer` links here.
    """

    __tablename__ = "customers"
    # The per-tenant unique constraint below leads with organization_id, so it
    # already serves org-only lookups.
    __org_index__ = False
    __table_args__ = (
        # Per-tenant uniqueness, matching the reasoning on User.email. Unnamed so
        # the naming convention generates a schema-unique index name.
        UniqueConstraint("organization_id", "email"),
        # Trigram index for fuzzy name search — supports ILIKE '%term%', which a
        # plain B-tree cannot serve. Requires the pg_trgm extension.
        Index(
            "ix_customers_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_customers_email_trgm",
            "email",
            postgresql_using="gin",
            postgresql_ops={"email": "gin_trgm_ops"},
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(50))

    # Identifier from an external CRM or billing system, for reconciliation.
    external_reference: Mapped[str | None] = mapped_column(String(200), index=True)

    # Flexible attributes (plan tier, VIP flag, account manager). JSONB rather
    # than JSON so it is queryable and indexable. Business rules read `vip` here
    # when computing effective ticket priority.
    #
    # `none_as_null` makes an explicit None a NOT NULL violation rather than a
    # stored JSON `null`, which would satisfy the constraint and then read back as
    # None — a value no caller expects from a non-nullable column.
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    organization: Mapped["Organization"] = relationship(back_populates="customers")
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    portal_users: Mapped[list["User"]] = relationship(back_populates="customer")

    def __repr__(self) -> str:
        return f"<Customer {self.email}>"
