"""Customer persistence."""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import InstrumentedAttribute

from app.models.customer import Customer
from app.repositories.base import TenantScopedRepository
from app.repositories.search import like_pattern
from app.schemas.customer import CustomerSortKey
from app.schemas.fields import SortOrder

# `CustomerSortKey` to the column it names. Keyed by the enum, so a member added with no
# column behind it is a type error rather than a `KeyError` at request time.
_CUSTOMER_SORT_COLUMNS: Mapping[CustomerSortKey, InstrumentedAttribute[Any]] = {
    CustomerSortKey.NAME: Customer.name,
    CustomerSortKey.CREATED_AT: Customer.created_at,
}


class CustomerRepository(TenantScopedRepository[Customer]):
    """Customers within the caller's organization.

    Not row-scoped. `docs/requirements.md` §3 gives customer access to admin, manager,
    and agent alike with no `own` or `assigned` qualifier — a customer record belongs to
    the organization, not to the agent who happens to deal with it — and gives the
    customer role no access at all, which is enforced by capability rather than by a
    scope.
    """

    model = Customer

    async def get_by_email(self, email: str) -> Customer | None:
        """Find a customer by email **within this tenant**.

        `email` must already be normalized (see `app/schemas/fields.py`); the unique
        constraint is case-sensitive and the stored column is lowercase.
        """
        result = await self.session.execute(self._select(Customer.email == email))
        return result.scalar_one_or_none()

    async def email_taken(self, email: str, *, excluding: uuid.UUID | None = None) -> bool:
        """Whether another customer in this tenant already holds this address.

        `excluding` is what makes this usable for an update: a customer keeping their
        own email is not a conflict with themselves.
        """
        criteria: list[ColumnElement[bool]] = [Customer.email == email]
        if excluding is not None:
            criteria.append(Customer.id != excluding)
        return await self.exists(*criteria)

    async def search(
        self,
        *,
        term: str | None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        sort: CustomerSortKey = CustomerSortKey.CREATED_AT,
        order: SortOrder = SortOrder.DESC,
        limit: int,
        offset: int,
    ) -> Sequence[Customer]:
        """A page of customers, optionally searched, date-filtered, and sorted.

        A substring match rather than a prefix one, because someone typing a support
        agent's memory of a customer is as likely to remember the domain or the second
        name as the first letter. Served by the two trigram indexes on the table, which
        a B-tree cannot do for `%term%`.

        `term=None` and `term=""` both mean "no filter" — a query string that arrives
        empty is not a search for the empty string, and matching on it would return
        everything anyway while giving the planner a pointless predicate.

        The date bounds are half-open — `created_after` inclusive, `created_before`
        exclusive — matching `/tickets` and `/audit-logs`, so a caller can walk adjacent
        windows without a boundary row appearing in both.

        **`Customer.id` is always the final sort key**, as it is for tickets. `name` in
        particular is not unique, and offset pagination over a non-unique sort without a
        tiebreak silently repeats one row and drops another.
        """
        criteria: list[ColumnElement[bool]] = []
        if term and term.strip():
            pattern = like_pattern(term)
            criteria.append(
                or_(
                    Customer.name.ilike(pattern, escape="\\"),
                    Customer.email.ilike(pattern, escape="\\"),
                )
            )
        if created_after is not None:
            criteria.append(Customer.created_at >= created_after)
        if created_before is not None:
            criteria.append(Customer.created_at < created_before)

        column = _CUSTOMER_SORT_COLUMNS[sort]
        direction = column.desc if order is SortOrder.DESC else column.asc

        statement = (
            self._select(*criteria).order_by(direction(), Customer.id).limit(limit).offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()
