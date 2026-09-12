"""User persistence.

Two ways in, and the split is the point:

* `UserRepository` — tenant-scoped, for everything an authenticated caller does.
* `find_users_by_email_across_tenants` — a module-level function for the login path
  only, where there is by definition no tenant yet. It is a function rather than a
  method so it cannot be reached for by accident while holding a scoped repository.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserRole
from app.models.user import User
from app.repositories.base import TenantScopedRepository


class UserRepository(TenantScopedRepository[User]):
    """Users within the caller's organization."""

    model = User

    async def get_by_email(self, email: str) -> User | None:
        """Find a user by email **within this tenant**.

        `email` must already be normalized (see `app/schemas/fields.py`); the stored
        column is lowercase, and a case-sensitive `=` against a mixed-case input would
        simply find nothing.
        """
        result = await self.session.execute(self._select(User.email == email))
        return result.scalar_one_or_none()

    async def email_taken(self, email: str) -> bool:
        return await self.exists(User.email == email)

    async def count_admins(self, *, excluding: uuid.UUID | None = None) -> int:
        """How many administrators this organization has.

        Used to refuse deactivating the last one — an organization with no admin has
        nobody who can create users, and no route back short of a database edit.
        """
        criteria = [User.role == UserRole.ADMIN]
        if excluding is not None:
            criteria.append(User.id != excluding)
        statement = self._select(*criteria).with_only_columns(func.count())
        # `int(...)` rather than a bare `or 0`: `scalar` is typed as returning `Any`,
        # and narrowing here is what keeps the return type honest.
        return int(await self.session.scalar(statement) or 0)

    async def list_users(self, *, limit: int, offset: int = 0) -> Sequence[User]:
        """A page of users, newest first.

        Overrides the base ordering deliberately: for people, "recently added" is the
        useful default, and `created_at` is monotonic enough to be a stable page key
        in practice. `id` is appended as a tiebreaker so two users created in the same
        millisecond still have a deterministic order.
        """
        statement = (
            self._select().order_by(User.created_at.desc(), User.id).limit(limit).offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()


async def find_users_by_email_across_tenants(session: AsyncSession, email: str) -> Sequence[User]:
    """Every user with this email, in any organization. **Login only.**

    This is the one query in the application that reads users without a tenant
    filter, and it exists because login has no tenant to filter by — the credentials
    are the only thing the client sends.

    Why a list rather than a single row: `users` is unique on
    `(organization_id, email)`, not on `email` alone, because the same real person may
    legitimately hold accounts with two different support providers. So an email can
    match more than one row, and the caller has to try the password against each
    candidate. The alternative — making email globally unique — would leak the
    existence of an account in one tenant to a user of another, which §11 forbids.

    Bounded at a small limit. A person with more than a handful of accounts under one
    address is not a case worth serving, and an unbounded result would mean an
    unbounded number of Argon2 derivations per login attempt.
    """
    result = await session.execute(
        select(User).where(User.email == email).order_by(User.created_at).limit(5)
    )
    return result.scalars().all()
