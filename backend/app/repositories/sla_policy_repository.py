"""SLA policy persistence — tenant-scoped, for everything reached from a request.

Two methods, and the small surface is the point: `SLAPolicy` has a unique constraint on
`(organization_id, priority)`, so there is no filtered list, no search, and no pagination
to build. A tenant has four rows and this reads them.

The worker reaches policies through `app/repositories/sla_repository.py` instead, which
has no `TenantContext` to construct one of these with — see that module's docstring.
"""

from collections.abc import Sequence

from app.core.exceptions import ErrorCode, NotFoundError
from app.models.enums import TicketPriority
from app.models.sla_policy import SLAPolicy
from app.repositories.base import TenantScopedRepository


class SLAPolicyRepository(TenantScopedRepository[SLAPolicy]):
    """This tenant's policies, and no other tenant's."""

    model = SLAPolicy

    async def list_all(self) -> Sequence[SLAPolicy]:
        """Every policy, active or not, in priority order.

        Ordered by the enum column rather than by `created_at`, which means PostgreSQL's
        enum declaration order — `LOW < MEDIUM < HIGH < URGENT`. That is the order an
        admin's settings screen wants to render, and it is the order §27 lists them in.
        The same trick `TicketSortKey.PRIORITY` relies on, and it is worth stating for the
        same reason: it is correct and it is not obvious.

        Inactive rows are included, unlike `list_active`. A settings screen has to show
        the targets a tenant switched off in order to offer switching them back on, and
        the alternative — an admin who cannot see the policy they disabled — makes
        `is_active` a one-way door.
        """
        result = await self.session.execute(self._select().order_by(SLAPolicy.priority))
        return result.scalars().all()

    async def list_active(self) -> Sequence[SLAPolicy]:
        """The policies the clock applies, keyed by nothing — callers index them.

        `is_active = false` is a tenant saying "do not enforce a target for this
        priority", and it is not the same as deleting the row: the targets survive and
        come back when it is switched on again, which is what the column's comment on the
        model promises.
        """
        statement = self._select(SLAPolicy.is_active.is_(True)).order_by(SLAPolicy.priority)
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def get_for_priority(self, priority: TicketPriority) -> SLAPolicy:
        """One priority's policy, or 404.

        Every organization is seeded with all four at registration
        (`sla_service.DEFAULT_POLICIES`), so a miss here means either a tenant created
        before this phase or one whose rows were deleted directly in the database. Both
        are 404 rather than an upsert: the endpoint's contract is "edit the policy that
        exists", and inventing a row from a request body would mean the response reported
        values the request supplied while claiming to report stored ones.
        """
        result = await self.session.execute(self._select(SLAPolicy.priority == priority))
        policy = result.scalar_one_or_none()
        if policy is None:
            raise NotFoundError(ErrorCode.SLA_POLICY_NOT_FOUND)
        return policy
