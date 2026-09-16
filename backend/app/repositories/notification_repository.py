"""Notification persistence — tenant-scoped, and then user-scoped.

Tenant isolation comes from the base class and is not restated here. What this
repository adds is the second, narrower rule the base class does not have: a
notification is reachable only by its own recipient.

**Why not `row_scope_predicate`.** `app/repositories/scoping.py` maps a role to a row
scope over *tickets* — `ORGANIZATION`, `ASSIGNED`, `OWN` — because a ticket's
visibility differs by role. A notification's does not. It is addressed to one user, and
every role reaches exactly their own, so the predicate is `user_id == context.user_id`
for all four roles with no mapping to consult. A scope table with the same value in
every row would suggest a rule that varies when it does not.

**Why the predicate lives here and not in the service.** ADR-015: row-level narrowing
belongs where the query is built, because that is the only place it cannot be
forgotten. A service calling `repo.get(id)` and then comparing `notification.user_id`
would work, and would also be one refactor away from a service that forgets. Here, the
lookup itself cannot return a colleague's row, so a wrong id is a 404 in the same body
as an id that was never written (ADR-009).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import ColumnElement, func, update
from sqlalchemy.engine import CursorResult

from app.models.notification import Notification
from app.repositories.base import TenantScopedRepository


class NotificationRepository(TenantScopedRepository[Notification]):
    """One user's notifications, and no others'."""

    model = Notification

    def _own(self, *criteria: ColumnElement[bool]) -> list[ColumnElement[bool]]:
        """The caller's own notifications, plus any further narrowing.

        Returned as a list rather than applied to a statement so every method below
        passes it through `_select`, which is what keeps the tenant predicate
        unavoidable. A method that built its own `select()` would lose it.
        """
        return [Notification.user_id == self.context.user_id, *criteria]

    async def get_for_user(self, notification_id: uuid.UUID) -> Notification | None:
        """The notification, if it is this caller's. Otherwise `None` — never a 403.

        The two failures are deliberately indistinguishable. A distinct error for "that
        is your colleague's" would confirm the row exists, which is exactly what an
        attacker walking a uuid space wants to learn, and it is the same reasoning that
        makes a cross-tenant read a 404 (ADR-009).
        """
        result = await self.session.execute(
            self._select(*self._own(self._id_column() == notification_id))
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self, *, unread_only: bool, limit: int, offset: int = 0
    ) -> Sequence[Notification]:
        """A page of the caller's notifications, newest first.

        `created_at DESC, id` — the `id` tiebreak makes the order total. Notifications
        are written in bursts (a bulk reassignment produces several rows sharing a
        timestamp to the microsecond), and offset pagination over a non-unique sort key
        repeats and skips rows. The same reasoning as `AuditLogRepository.list_logs`.
        """
        criteria = [Notification.read_at.is_(None)] if unread_only else []
        statement = (
            self._select(*self._own(*criteria))
            .order_by(Notification.created_at.desc(), Notification.id)
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def count_unread(self) -> int:
        """How many of the caller's notifications are unread.

        A `COUNT` rather than `len(await list_for_user(...))`: the badge asks a question
        whose answer is one number, and materializing every unread row to measure it
        makes the cost of the cheapest endpoint grow with the user's backlog. Backed by
        `ix_notifications_user_unread`, which is partial on `read_at IS NULL` for
        exactly this query.
        """
        statement = self._select(*self._own(Notification.read_at.is_(None))).with_only_columns(
            func.count()
        )
        return await self.session.scalar(statement) or 0

    async def mark_all_read(self) -> int:
        """Mark every unread notification of the caller's as read. Returns the count.

        A set-based `UPDATE` rather than a fetch-then-loop: "mark all read" is one
        statement's worth of work whatever the size of the backlog, and a loop would
        issue one `UPDATE` per row for a button whose whole purpose is to clear a large
        number at once.

        The `read_at IS NULL` predicate is not an optimization — it is what stops the
        statement from rewriting the timestamp on notifications the user read last week.
        `mark all read` means "mark the unread ones", and re-stamping read rows would
        silently move every read time in the history to now.

        The timestamp is computed once in Python and sent as one value, so every row the
        statement touches is stamped identically. That is the behaviour a user expects
        from "mark all read" — one act, one time — and it matches `mark_read`, which uses
        the same clock for the same reason. The schema's `created_at` columns use
        `server_default=now()` because they are defaults the database owns; an explicit
        application-set timestamp is the application's to choose.

        The count is the statement's own row count, which is the number of rows it
        actually changed.
        """
        statement = (
            update(Notification)
            .where(
                self._tenant_column() == self.organization_id,
                *self._own(Notification.read_at.is_(None)),
            )
            .values(read_at=datetime.now(UTC))
        )
        # `AsyncSession.execute` is typed as returning `Result`, which has no `rowcount`;
        # an `UPDATE` returns a `CursorResult`, which does. The cast is the type system's
        # gap rather than a claim about the runtime: `rowcount` is exactly the number of
        # rows the statement changed, which is what this method promises to return.
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return result.rowcount or 0

    def mark_read(self, notification: Notification) -> Notification:
        """Stamp one notification as read. Never commits — the caller owns the commit.

        Takes the loaded row rather than an id so the service resolves visibility first
        (`get_for_user`) and this stays a pure field assignment. Passing an id here
        would mean a second lookup, and the second lookup would be the one that could
        forget the user predicate.
        """
        notification.read_at = datetime.now(UTC)
        return notification
