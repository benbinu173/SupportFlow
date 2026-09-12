"""Customer persistence."""

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, or_

from app.models.customer import Customer
from app.repositories.base import TenantScopedRepository


def escape_like(term: str) -> str:
    """Escape the wildcards in a user-supplied search term.

    SQLAlchemy parameterizes the value, so this is not about injection — a `%` in a
    bind parameter is data and nothing more. It is about *meaning*: `%` and `_` are
    LIKE metacharacters, so a customer searching for `50%` would otherwise match every
    record in the table, and one searching for `a_b` would match `aXb`. The backslash
    is escaped first, since it is the escape character itself and escaping it later
    would double up on the escapes added before it.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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

    async def search(self, *, term: str | None, limit: int, offset: int) -> Sequence[Customer]:
        """A page of customers, newest first, optionally filtered by name or email.

        A substring match rather than a prefix one, because someone typing a support
        agent's memory of a customer is as likely to remember the domain or the second
        name as the first letter. Served by the two trigram indexes on the table, which
        a B-tree cannot do for `%term%`.

        `term=None` and `term=""` both mean "no filter" — a query string that arrives
        empty is not a search for the empty string, and matching on it would return
        everything anyway while giving the planner a pointless predicate.
        """
        criteria: list[ColumnElement[bool]] = []
        if term and term.strip():
            pattern = f"%{escape_like(term.strip())}%"
            criteria.append(
                or_(
                    Customer.name.ilike(pattern, escape="\\"),
                    Customer.email.ilike(pattern, escape="\\"),
                )
            )

        statement = (
            self._select(*criteria)
            .order_by(Customer.created_at.desc(), Customer.id)
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()
