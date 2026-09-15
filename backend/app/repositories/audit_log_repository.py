"""Audit log persistence — append-only, and structurally so.

`AuditLog` carries no `updated_at` and the application never updates or deletes a row.
That is a claim, and a claim about a table is worth less than the code that makes it
true, so the repository has exactly two capabilities: **add a row** and **read rows**.
There is no `update`, no `delete`, and no way to reach one through this class — a test
asserts the absence rather than trusting the intent.

Spec §34 asks for a viewer ("Add audit viewer for admin"), which is the only read here.
It is tenant-scoped like every other repository: `_select` carries the organization
predicate, so an admin reads their own organization's trail and no other.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement

from app.models.audit_log import AuditLog
from app.models.enums import AuditAction
from app.repositories.base import TenantScopedRepository


class AuditLogRepository(TenantScopedRepository[AuditLog]):
    """Rows in the caller's organization, newest first.

    Reads only. See the module docstring.
    """

    model = AuditLog

    async def list_logs(
        self,
        *,
        action: AuditAction | None = None,
        actor_user_id: uuid.UUID | None = None,
        target_type: str | None = None,
        target_id: uuid.UUID | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int,
        offset: int = 0,
    ) -> Sequence[AuditLog]:
        """A page of the audit trail, newest first.

        Ordered `created_at DESC, id` — and the `id` tiebreak is not decoration. Several
        audit rows can share a `created_at` to the microsecond when one request writes
        more than one, and offset pagination over a non-unique sort key silently repeats
        and skips rows. The `id` makes the order total, so page 2 starts exactly where
        page 1 ended.

        Each filter is independent and narrows; none can widen the tenant predicate,
        which `_select` applies first and this method cannot remove.
        """
        criteria: list[ColumnElement[bool]] = []
        if action is not None:
            criteria.append(AuditLog.action == action)
        if actor_user_id is not None:
            criteria.append(AuditLog.actor_user_id == actor_user_id)
        if target_type is not None:
            criteria.append(AuditLog.target_type == target_type)
        if target_id is not None:
            criteria.append(AuditLog.target_id == target_id)
        # Half-open, like the ticket and customer list filters: `after` is inclusive and
        # `before` exclusive, so adjacent windows can be walked without double-counting.
        if created_after is not None:
            criteria.append(AuditLog.created_at >= created_after)
        if created_before is not None:
            criteria.append(AuditLog.created_at < created_before)

        statement = (
            self._select(*criteria)
            .order_by(AuditLog.created_at.desc(), AuditLog.id)
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()
