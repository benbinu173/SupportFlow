"""Ticket persistence, including row scope and the per-tenant number allocation."""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.orm import InstrumentedAttribute

from app.core.permissions import TICKET_SCOPE_BY_ROLE, Permission
from app.models.enums import TicketPriority, TicketStatus
from app.models.ticket import Ticket
from app.repositories.base import TenantScopedRepository
from app.repositories.scoping import row_scope_predicate
from app.repositories.search import ticket_search_predicate
from app.schemas.fields import SortOrder
from app.schemas.ticket import TicketSortKey


def _lock_key(organization_id: uuid.UUID) -> int:
    """A 64-bit advisory-lock key derived from an organization id.

    The first eight bytes of the UUID, read as a signed big-endian integer — arbitrary
    but deterministic, which is all an advisory lock needs. It is derived rather than
    allocated because there is nothing to allocate: the lock exists only for the
    duration of one transaction, and a collision between two organizations would merely
    serialize two ticket creations that did not need to be serialized. It cannot cause
    a wrong answer, which is the property that matters.
    """
    return int.from_bytes(organization_id.bytes[:8], "big", signed=True)


# `TicketSortKey` to the column it names. Keyed by the enum so mypy reports a member
# added to `TicketSortKey` with no entry here, which a `match` over strings would not.
#
# `Any` in the value position because the four columns have four different Python types
# (`datetime`, `datetime`, `int`, `TicketPriority`). The element type is uniform in the
# way that matters — every one of them is a mapped attribute that can be ordered.
_TICKET_SORT_COLUMNS: Mapping[TicketSortKey, InstrumentedAttribute[Any]] = {
    TicketSortKey.CREATED_AT: Ticket.created_at,
    TicketSortKey.UPDATED_AT: Ticket.updated_at,
    TicketSortKey.NUMBER: Ticket.number,
    TicketSortKey.PRIORITY: Ticket.priority,
}


class TicketRepository(TenantScopedRepository[Ticket]):
    """Tickets within the caller's organization, narrowed to their row scope.

    The two filters answer different questions and both are always applied:
    `TenantScopedRepository` answers "which organization", and `row_scope_predicate`
    answers "how much of it". An agent's `GET /tickets` therefore carries
    `organization_id = :org AND assigned_agent_id = :me`, and neither predicate can be
    dropped by a caller.
    """

    model = Ticket

    def _scope(self) -> ColumnElement[bool]:
        return row_scope_predicate(
            self.context,
            TICKET_SCOPE_BY_ROLE,
            owner_column=Ticket.customer_id,
            assignee_column=Ticket.assigned_agent_id,
        )

    async def get_visible(self, ticket_id: uuid.UUID) -> Ticket | None:
        """One ticket, if the caller may reach it at all.

        Named `get_visible` rather than overriding `get` so that a subclass — or a
        future reader — cannot mistake the scoped read for the tenant-only one. A
        ticket assigned to a colleague returns `None` here, which the service turns into
        the same 404 as a ticket in another organization: an agent should not be able to
        discover the existence of a colleague's ticket by the shape of the refusal.
        """
        result = await self.session.execute(self._select(self._scope(), Ticket.id == ticket_id))
        return result.scalar_one_or_none()

    async def list_tickets(
        self,
        *,
        status: TicketStatus | None = None,
        priority: TicketPriority | None = None,
        assigned_agent_id: uuid.UUID | None = None,
        unassigned: bool = False,
        customer_id: uuid.UUID | None = None,
        term: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        sort: TicketSortKey = TicketSortKey.CREATED_AT,
        order: SortOrder = SortOrder.DESC,
        limit: int,
        offset: int = 0,
    ) -> Sequence[Ticket]:
        """A page of tickets the caller may reach, filtered, searched, and sorted.

        The filters are **narrowing only**. They compose with the scope predicate and
        cannot widen it: an agent asking for `assigned_agent_id=<someone else>` gets an
        empty page, not that agent's queue, because the scope predicate is applied
        regardless and both conditions must hold. That is the property
        `tests/security/test_row_scopes.py` asserts directly, since "a query parameter
        that widens access" is the classic version of this bug. `term` is subject to the
        same rule, which is what `tests/api/test_search.py` checks from the other side.

        `unassigned` is the second half of a tri-state the route resolves: the caller
        passes either an agent id, or `unassigned=True`, or neither. Both at once is
        refused at the route rather than resolved here, because "which filter wins" has
        no defensible answer and a repository is the wrong place to invent one.

        `created_after` is inclusive and `created_before` exclusive, so adjacent windows
        tile without overlap or gap. The direction is a decision, not an accident: a
        caller paging through time needs the boundary row in exactly one window, and
        half-open is the only convention under which that holds for a `timestamptz`.

        **Ordering always ends with `Ticket.id`.** Without a unique tiebreak, offset
        pagination over equal sort values repeats and skips rows - two tickets created in
        the same transaction share a `created_at` to the microsecond, and the page that
        ends on one of them is not the page that continues from it. The bug only appears
        under load, which is exactly when it is not being looked for.
        """
        criteria: list[ColumnElement[bool]] = [self._scope()]
        if status is not None:
            criteria.append(Ticket.status == status)
        if priority is not None:
            criteria.append(Ticket.priority == priority)
        if assigned_agent_id is not None:
            criteria.append(Ticket.assigned_agent_id == assigned_agent_id)
        if unassigned:
            criteria.append(Ticket.assigned_agent_id.is_(None))
        if customer_id is not None:
            criteria.append(Ticket.customer_id == customer_id)
        if created_after is not None:
            criteria.append(Ticket.created_at >= created_after)
        if created_before is not None:
            criteria.append(Ticket.created_at < created_before)
        if term and term.strip():
            # ANDed with the scope predicate above, never substituted for it. The
            # `include_internal` argument comes from the caller's capability rather than
            # from the request, so a customer cannot ask for the internal arm.
            criteria.append(
                ticket_search_predicate(
                    term,
                    include_internal=self.context.has(Permission.MESSAGE_READ_INTERNAL),
                )
            )

        column = _TICKET_SORT_COLUMNS[sort]
        direction = column.desc if order is SortOrder.DESC else column.asc

        statement = (
            self._select(*criteria).order_by(direction(), Ticket.id).limit(limit).offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def allocate_number(self) -> int:
        """The next ticket number for this organization.

        Two concurrent inserts both reading `MAX(number) + 1` would pick the same value,
        and the unique index on `(organization_id, number)` would reject one of them —
        which is the failure mode `app/models/ticket.py` anticipates when it says the
        collision is "a retryable integrity error".

        This takes the other route: a transaction-scoped advisory lock keyed on the
        organization, held until commit or rollback. That makes the collision impossible
        rather than merely detectable, and avoids a retry loop's real complications — a
        failed statement aborts the transaction, so a retry needs a `SAVEPOINT`, a
        bounded attempt count, and a test for the exhaustion path. ADR-016 records the
        trade: ticket creation within one tenant serializes, which is a short
        transaction on a low-frequency write, and two tenants never contend.

        The unique index stays. It is the invariant; this is the mechanism.
        """
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(self.organization_id)}
        )
        statement = select(func.coalesce(func.max(Ticket.number), 0) + 1).where(
            Ticket.organization_id == self.organization_id
        )
        return int(await self.session.scalar(statement) or 1)
