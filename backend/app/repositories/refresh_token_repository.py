"""Refresh token persistence and revocation.

Lookups here are by token *hash*, not by tenant. That is not an oversight: the hash is
the credential, so it is the only thing the refresh request carries — the tenant is
read off the row once it is found. This repository therefore extends the plain
`Repository` rather than the tenant-scoped one, and the class docstring says so out
loud because it is the kind of thing a reader should question and then find answered.

Every method that reads a token takes `for_update=True` by default. Two concurrent
refreshes presenting the same token must not both succeed — the row lock is what makes
"mark used, then issue a replacement" atomic. Without it, both requests read an
unrevoked row and both mint a new session, and the reuse detector never fires because
neither saw a conflict.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update

from app.models.refresh_token import RefreshToken
from app.repositories.base import Repository


class RefreshTokenRepository(Repository[RefreshToken]):
    model = RefreshToken

    async def find_by_hash(
        self, token_hash: str, *, for_update: bool = True
    ) -> RefreshToken | None:
        """Fetch a token row by its hash, revoked or not.

        Returns revoked and expired rows too, deliberately. The caller needs to tell
        "no such token" from "this token was already used", because the second case is
        a theft signal that revokes the whole session family — a lookup that filtered
        `revoked_at IS NULL` would collapse the two and silently swallow the alarm.
        """
        statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        if for_update:
            # Skip the lock when the row is already gone rather than blocking on a
            # concurrent delete; the caller handles `None` either way.
            statement = statement.with_for_update(skip_locked=True)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    def add(self, token: RefreshToken) -> RefreshToken:
        self.session.add(token)
        return token

    async def revoke(self, token: RefreshToken) -> None:
        """Mark one token used, without committing."""
        token.revoked_at = datetime.now(UTC)

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Revoke every live token belonging to a user. Returns how many were revoked.

        Deliberately broader than the `replaced_by_id` chain ADR-003 describes. On
        reuse detection the honest statement is "one of this user's sessions has been
        compromised, and we do not know which" — the replayed token proves theft but
        not which holder is the thief. Killing the whole family kills every other live
        session too, and that is the correct trade: the user signs in again, which is
        a small cost, versus an attacker retaining a valid session, which is not.

        A single `UPDATE` rather than a chain walk, so it cannot be foiled by a broken
        link or a row that was cleaned up by the expiry sweep.
        """
        result = await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        # `rowcount` lives on `CursorResult`, which async `execute()` does not advertise
        # even though it returns one for a DML statement. And with SQLAlchemy's
        # synchronize_session default the value is authoritative for an UPDATE.
        return int(cast("CursorResult[Any]", result).rowcount)
