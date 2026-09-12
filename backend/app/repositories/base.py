"""Repository base classes.

`TenantScopedRepository` is the single most important class in the multi-tenancy
story. It holds a `TenantContext` and every query it builds carries the tenant
predicate, so a repository simply cannot read across organizations — there is no
unscoped `_select` to reach for. Making isolation structural rather than a convention
each new query has to remember is the whole point (architecture §11).

Both classes are generic over their model, so `scalar_one_or_none()` returns the
concrete entity type. A non-generic base returning `Any` would type-check at the
definition and silently erase the type at every call site.
"""

import uuid
from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.models.base import Base


class Repository[EntityT: Base]:
    """A repository over a single entity, with no tenant filter.

    Only for tables that are not tenant-owned — `organizations`, which *is* the
    tenant, and lookups on `refresh_tokens` that are keyed by the token hash itself
    and therefore happen before a tenant is known. Anything else belongs on a
    `TenantScopedRepository`.
    """

    model: ClassVar[type[Any]]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session


class TenantScopedRepository[EntityT: Base](Repository[EntityT]):
    """A repository whose every query is filtered to one organization.

    Constructed from the request's `TenantContext`, never from anything the client
    supplied. There is deliberately no method that omits the tenant predicate: the
    escape hatch would be found and used.
    """

    # Narrowed from the base's `type[Any]` so subclasses get their own entity type
    # back out of `get()` and `list()`.
    model: ClassVar[type[EntityT]]

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        super().__init__(session)
        self.context = context

    @property
    def organization_id(self) -> uuid.UUID:
        """The tenant this repository is pinned to. From the token, never a request."""
        return self.context.organization_id

    def _tenant_column(self) -> Any:
        """The model's `organization_id` mapped attribute.

        `getattr` rather than `self.model.organization_id` because the column arrives
        through `OrganizationScopedMixin`'s `declared_attr`, which the type checker
        cannot see on a `TypeVar` bound to `Base`. Keeping the one escape hatch here
        means no subclass needs its own, and the `Any` return is narrow enough that it
        cannot hide a mistake elsewhere.
        """
        return getattr(self.model, "organization_id")  # noqa: B009

    def _id_column(self) -> Any:
        """The model's `id` mapped attribute.

        Same reason as `_tenant_column`: `UUIDPrimaryKeyMixin` supplies `id`, but
        through a mixin the `TypeVar` bound cannot see.
        """
        return getattr(self.model, "id")  # noqa: B009

    def _select(self, *criteria: ColumnElement[bool]) -> Select[tuple[EntityT]]:
        """A `SELECT` already filtered to this tenant, plus any extra criteria.

        The tenant predicate is applied first and cannot be removed by a caller. Every
        read in a subclass should start here.
        """
        statement = select(self.model).where(self._tenant_column() == self.organization_id)
        if criteria:
            statement = statement.where(*criteria)
        return statement

    async def get(self, entity_id: uuid.UUID) -> EntityT | None:
        """Fetch one row by id, **within the tenant**.

        Returns `None` for an id that belongs to another organization, exactly as it
        does for an id that does not exist. That is the intended behaviour, not an
        oversight: distinguishing the two would turn the endpoint into an oracle for
        which ids are real (ADR-009). Callers surface this as 404.
        """
        result = await self.session.execute(self._select(self._id_column() == entity_id))
        return result.scalar_one_or_none()

    async def list(self, *, limit: int, offset: int = 0) -> Sequence[EntityT]:
        """A page of rows in this tenant.

        Ordered by id for a stable page sequence. Without an explicit `ORDER BY`,
        PostgreSQL is free to return rows in any order it likes — and it does, once a
        table is large enough to change plans — so offset pagination without one can
        skip and duplicate rows.
        """
        statement = self._select().order_by(self._id_column()).limit(limit).offset(offset)
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def exists(self, *criteria: ColumnElement[bool]) -> bool:
        """Whether any row in this tenant matches."""
        return bool(await self.session.scalar(select(self._select(*criteria).exists())))

    def add(self, entity: EntityT) -> EntityT:
        """Stage a new row. The caller commits.

        No `flush` here: flushing is a decision about when the database sees the
        write, and the service layer owns transaction boundaries.
        """
        self.session.add(entity)
        return entity
