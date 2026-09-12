"""Declarative base and shared column mixins.

Every model inherits `Base`. Most inherit `UUIDPrimaryKeyMixin` and
`TimestampMixin`; tenant-owned models also inherit `OrganizationScopedMixin`,
which is what makes the isolation guarantee structural rather than conventional.
"""

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Explicit naming conventions so Alembic autogenerate produces stable, readable
# names instead of relying on database defaults. Without this, dropping an
# unnamed constraint in a downgrade requires knowing what Postgres chose.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # `updated_at` is set by an `onupdate=func.now()` expression, and SQLAlchemy has no
    # way to know what `now()` returned — so after an UPDATE it *expires* the attribute,
    # meaning the next read of it emits a fresh SELECT. That read happens when a route
    # serializes the row it just changed, which is outside the async greenlet and fails
    # with `MissingGreenlet` rather than with anything that names the real cause.
    #
    # `eager_defaults` asks the database for the value in the statement that changed it
    # (`UPDATE … RETURNING updated_at`, which Postgres supports) instead of discarding
    # it. Set on the declarative base so every mapped class inherits it: the alternative
    # is an `await session.refresh(row)` after each of a dozen commits, which costs a
    # round-trip per write and is only correct where someone remembered to add it.
    # `ClassVar` because it is class-level configuration, not per-instance state — ruff
    # requires the annotation for a mutable class attribute. mypy objects that
    # `DeclarativeBase` declares `__mapper_args__` as an instance attribute; that is a
    # limitation of SQLAlchemy's stubs, since the mapper reads it from the class.
    __mapper_args__: ClassVar[dict[str, Any]] = {  # type: ignore[misc]
        "eager_defaults": True
    }


class UUIDPrimaryKeyMixin:
    """UUID primary keys.

    Chosen over sequential integers so identifiers are not guessable: a
    multi-tenant system should not let a client enumerate records by
    incrementing an ID. Generated database-side via gen_random_uuid().
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """Creation and update timestamps.

    Both are timezone-aware and set by the database, so values stay correct
    regardless of the application server's clock or timezone. `onupdate` fires on
    ORM flush; `server_default` covers rows written outside the ORM.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrganizationScopedMixin:
    """Tenant ownership.

    Applied to every tenant-owned table. The column is non-nullable because an
    unowned row could not be safely filtered, and ON DELETE CASCADE means removing
    an organization removes its data rather than orphaning it.

    Indexed by default, and skipped via `__org_index__ = False` on the tables where
    another index already *leads* with `organization_id`. A B-tree on
    `(organization_id, …)` serves a predicate on `organization_id` alone, so the
    standalone index would be maintained on every insert for nothing. The default
    stays "indexed" so a new tenant table cannot silently lose the index; opting out
    is a deliberate, reviewed act.
    """

    # Class-level opt-out, read by the declared_attr below. No annotation, so
    # SQLAlchemy does not mistake it for a mapped attribute.
    __org_index__ = True

    @declared_attr
    def organization_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            UUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=getattr(cls, "__org_index__", True),
        )
