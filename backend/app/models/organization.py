"""Organization — the tenant root."""

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    ORGANIZATION_PLAN_ENUM,
    ORGANIZATION_STATUS_ENUM,
    OrganizationPlan,
    OrganizationStatus,
)

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.knowledge_document import KnowledgeDocument
    from app.models.sla_policy import SLAPolicy
    from app.models.ticket import Ticket
    from app.models.user import User


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant. Every other business record traces ownership back to one of these.

    This table alone is not organization-scoped — it *is* the scope.
    """

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Globally unique: used in URLs and as a human-readable tenant identifier.
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)

    plan: Mapped[OrganizationPlan] = mapped_column(
        ORGANIZATION_PLAN_ENUM,
        nullable=False,
        default=OrganizationPlan.FREE,
        server_default=OrganizationPlan.FREE.value,
    )
    status: Mapped[OrganizationStatus] = mapped_column(
        ORGANIZATION_STATUS_ENUM,
        nullable=False,
        default=OrganizationStatus.ACTIVE,
        server_default=OrganizationStatus.ACTIVE.value,
        index=True,
    )

    # Cascade deletes: removing a tenant removes its data rather than orphaning it.
    users: Mapped[list["User"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    customers: Mapped[list["Customer"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    knowledge_documents: Mapped[list["KnowledgeDocument"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    sla_policies: Mapped[list["SLAPolicy"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Organization {self.slug}>"
