"""Analytics aggregates — five endpoints' worth of SQL, and one fragment worth explaining.

**Everything here is a `TenantScopedRepository`, so the tenant predicate is not something
this module has to remember.** Every statement starts at `_select`, which applies
`organization_id` first and cannot be removed, and every one of them also carries
`_scope()` — `row_scope_predicate` over `TICKET_SCOPE_BY_ROLE`, the same predicate
`GET /tickets` uses. An agent's analytics are therefore their assigned work and a
manager's are the organization's, from one implementation and no second scope map.

**The SQL `GROUP BY` is the point of the phase.** §28 asks for aggregation in PostgreSQL,
§53 lists "slow analytics" among the things to avoid, and the indexes Phase D built for
these exact predicates (`ix_tickets_org_status_priority`, `ix_tickets_org_category`,
`ix_tickets_org_agent_status_created`, `ix_tickets_org_created_at`) are what let the
database do the counting rather than the application. No new index is added: a query that
needed one would say the existing ones were chosen wrong.

`COUNT` and `with_only_columns` follow
`NotificationRepository.count_unread`, which records why measuring a number by
materializing rows is the wrong shape.

The one fragment
----------------
`due_at` below is the deadline instant of a ticket's timer, expressed in SQL: the same
`started_at + timedelta(minutes=target_minutes)` that `sla_service.resolve_timer` computes,
from the same two operands — `tickets.created_at` and the tenant's active policy for the
ticket's priority. It is used to **rank** open tickets by how close they are to a deadline
(`risk_candidates`), to **count** how many are past one (`overdue_count`), and to
**classify** a stopped timer as met or missed (`sla_compliance`).

Ranking is the reason it exists at all. "The ten open tickets nearest a deadline" cannot be
answered without the arithmetic in the query — the alternatives are reading every open
ticket (unbounded, §7) or ordering by `created_at` and calling the result a risk list,
which is wrong the moment two priorities have different targets.

**Every number a client is shown still comes from `resolve_position`.** This module decides
which rows and in what order; `app/services/analytics_service.py` decides the values. A
disagreement between the two costs a wrong position in a list, never a wrong number on a
screen — and `tests/integration/test_analytics_sla_agreement.py` asserts they agree at every
boundary, in both directions, which is what replaces "there is only one implementation".

**The warning instant is deliberately absent.** `sla_service._warning_offset` multiplies a
`timedelta` by a float, so putting it in SQL means integer minutes and a possible
few-seconds disagreement at the band edge. Anything that needs the warning band is answered
by the clock instead, which is why the risk list is computed per request and uncached.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, case, distinct, func, select
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql import Select

from app.core.permissions import TICKET_SCOPE_BY_ROLE
from app.models.ai_usage import AIUsage
from app.models.enums import Sentiment, TicketPriority, TicketStatus
from app.models.sla_policy import SLAPolicy
from app.models.ticket import Ticket
from app.models.user import User
from app.repositories.base import TenantScopedRepository
from app.repositories.scoping import row_scope_predicate
from app.repositories.sla_repository import TERMINAL_STATUSES

#: How the SLA methods join a ticket to its organization's active policy. The tenant
#: predicate is written out here as well as being in the `WHERE`, so the join cannot match
#: a policy belonging to another organization even if a ticket's `priority` somehow
#: pointed at one — the same "visible at the join rather than inherited by luck" reasoning
#: the `by_agent` join below follows.
#:
#: `is_active` is part of the join and not a filter, so a priority the tenant switched off
#: has no clock — which is the answer `sla_service.load_policies` gives it too. Such a
#: ticket is absent from every SLA number below rather than counted as on time.
#:
#: `resolution_time_minutes >= response_time_minutes` is a `CheckConstraint`, so the
#: response deadline is never the later of the two. Nothing below relies on that, but it is
#: why the two deadlines are comparable at all.
_POLICY_JOIN = (
    SLAPolicy.organization_id == Ticket.organization_id,
    SLAPolicy.priority == Ticket.priority,
    SLAPolicy.is_active.is_(True),
)


def _due_at(target_minutes: InstrumentedAttribute[Any]) -> ColumnElement[datetime]:
    """`resolve_timer`'s `due_at`, in the query language.

    `started_at + timedelta(minutes=target_minutes)`. PostgreSQL's `make_interval` takes
    the units positionally; only `mins` is used, and the column is an `integer` on both
    sides, so there is no rounding to disagree about.

    The parameter is an `InstrumentedAttribute[Any]` rather than a `ColumnElement[int]`
    because that is what a mapped column *is* — the same annotation `row_scope_predicate`
    takes its columns under, and the same reason: `Any` here is SQLAlchemy's typing of
    mapped attributes, not a claim that this accepts anything.
    """
    return Ticket.created_at + func.make_interval(0, 0, 0, 0, 0, target_minutes)


def _unstopped_due_at(
    stopped_column: InstrumentedAttribute[Any], target_minutes: InstrumentedAttribute[Any]
) -> ColumnElement[datetime | None]:
    """The timer's deadline, or NULL when the timer has already stopped.

    `CASE WHEN stopped_at IS NULL THEN due_at ELSE NULL END`. The NULL is not a
    convenience: `LEAST` ignores NULL arguments, so wrapping both timers' deadlines this
    way is what makes a `least(...)` over them mean "the soonest deadline still
    outstanding" rather than "the soonest deadline of a timer that may already be met".
    """
    return case((stopped_column.is_(None), _due_at(target_minutes)), else_=None)


class AnalyticsRepository(TenantScopedRepository[Ticket]):
    """Aggregates over the caller's tickets, narrowed to their row scope."""

    model = Ticket

    def _scope(self) -> ColumnElement[bool]:
        """The same row scope every ticket read applies. Copied from `TicketRepository`.

        Copied rather than shared because the two classes are siblings, not parent and
        child, and both call the same function with the same two columns — which is what
        makes them the same rule. A shared base class would be a third place to look for
        it.
        """
        return row_scope_predicate(
            self.context,
            TICKET_SCOPE_BY_ROLE,
            owner_column=Ticket.customer_id,
            assignee_column=Ticket.assigned_agent_id,
        )

    def _scoped(self, *criteria: ColumnElement[bool]) -> Select[tuple[Ticket]]:
        """`_select` plus the row scope — where every statement below starts."""
        return self._select(self._scope(), *criteria)

    @staticmethod
    def _in_window(start: datetime, end: datetime) -> list[ColumnElement[bool]]:
        """`created_at` in `[start, end)`. The half-open convention, for the reason
        `TicketRepository.list_tickets` gives: adjacent windows must tile."""
        return [Ticket.created_at >= start, Ticket.created_at < end]

    async def _count(self, statement: Select[Any]) -> int:
        """One number from one statement."""
        return int(await self.session.scalar(statement.with_only_columns(func.count())) or 0)

    # -----------------------------------------------------------------------
    # Tickets
    # -----------------------------------------------------------------------

    async def status_counts(self, start: datetime, end: datetime) -> Mapping[TicketStatus, int]:
        """How many tickets of each status were created in the window.

        A mapping rather than a sequence, because the caller has to fill the *missing*
        members with zeros to make the breakdown complete, and doing that from a dict is
        one comprehension instead of a lookup per member.

        Groups on the enum column directly, so the values are `TicketStatus` members and
        not strings — SQLAlchemy converts them on the way out, since the column is a
        native PostgreSQL enum.
        """
        statement = (
            self._scoped(*self._in_window(start, end))
            .with_only_columns(Ticket.status, func.count())
            .group_by(Ticket.status)
        )
        result = await self.session.execute(statement)
        return {row[0]: int(row[1]) for row in result.all()}

    async def volume(self, start: datetime, end: datetime) -> Sequence[tuple[datetime, int]]:
        """§28's "ticket volume over time", one row per UTC day that has tickets.

        `date_trunc('day', created_at)` — the session's timezone decides where the day
        starts, and the container runs UTC. Nothing in the schema carries a tenant's
        timezone, so a local-midnight bucket is not available to be asked for.

        **Days with no tickets are absent, not zero.** Filling them here would mean
        generating a series in SQL (`generate_series`) for every request, and the caller
        that renders a chart is the one that knows which days it wants to plot. The README
        records it as a limitation rather than this method inventing a calendar.
        """
        bucket = func.date_trunc("day", Ticket.created_at)
        statement = (
            self._scoped(*self._in_window(start, end))
            .with_only_columns(bucket.label("bucket"), func.count())
            .group_by(bucket)
            .order_by(bucket)
        )
        result = await self.session.execute(statement)
        return [(row[0], int(row[1])) for row in result.all()]

    async def duration_averages(
        self, start: datetime, end: datetime
    ) -> Mapping[str, tuple[int, int]]:
        """§28's two averages, in one round trip. Keyed `response` and `resolution`.

        Each is `(average_seconds, count)` over the tickets in the window whose timer has
        stopped — an unstopped timer has no duration, and folding it in as zero would
        report a fast desk for a tenant with a backlog.

        `FILTER (WHERE ...)` rather than two statements: the two averages are over the same
        rows with different NULL-ness, so one scan answers both.

        The interval is `stopped_at - created_at`, which is where both SLA timers start.
        That is the same start point `resolve_position` uses, so this average and the
        compliance percentages below describe one clock rather than two.
        """
        response = Ticket.first_response_at
        resolved = Ticket.resolved_at

        statement = self._scoped(*self._in_window(start, end)).with_only_columns(
            func.avg(func.extract("epoch", response - Ticket.created_at)).filter(
                response.is_not(None)
            ),
            func.count().filter(response.is_not(None)),
            func.avg(func.extract("epoch", resolved - Ticket.created_at)).filter(
                resolved.is_not(None)
            ),
            func.count().filter(resolved.is_not(None)),
        )
        row = (await self.session.execute(statement)).one()

        def pair(average: Any, count: Any) -> tuple[int, int]:
            # `None` average with a zero count is the no-rows case, and the caller renders
            # it as `null` rather than as zero seconds.
            return (int(average) if average is not None else 0, int(count))

        return {"response": pair(row[0], row[1]), "resolution": pair(row[2], row[3])}

    async def priority_counts(self, start: datetime, end: datetime) -> Mapping[TicketPriority, int]:
        """§28's "tickets by priority". A mapping, for the same reason as `status_counts`."""
        statement = (
            self._scoped(*self._in_window(start, end))
            .with_only_columns(Ticket.priority, func.count())
            .group_by(Ticket.priority)
        )
        result = await self.session.execute(statement)
        return {row[0]: int(row[1]) for row in result.all()}

    async def category_counts(
        self, start: datetime, end: datetime, *, limit: int
    ) -> Sequence[tuple[str | None, int]]:
        """§28's "tickets by category", highest volume first.

        `category` is free text written by AI classification (Phase U), so unlike a status
        or a priority the set of keys is not known ahead of time and the breakdown is a
        top-N rather than a complete table. Until Phase U runs, every ticket is in the
        `None` bucket — which is the truth about this tenant, not a placeholder.

        The `NULLS LAST` tiebreak matters for offset-free paging only in principle here,
        but ordering by a non-unique `count` alone would make two requests for the same
        window able to disagree about which category is fifth.
        """
        statement = (
            self._scoped(*self._in_window(start, end))
            .with_only_columns(Ticket.category, func.count())
            .group_by(Ticket.category)
            .order_by(func.count().desc(), Ticket.category.asc().nulls_last())
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return [(row[0], int(row[1])) for row in result.all()]

    async def sentiment_counts(
        self, start: datetime, end: datetime
    ) -> Mapping[Sentiment | None, int]:
        """§28's "sentiment distribution". The `None` key is "not analysed yet"."""
        statement = (
            self._scoped(*self._in_window(start, end))
            .with_only_columns(Ticket.sentiment, func.count())
            .group_by(Ticket.sentiment)
        )
        result = await self.session.execute(statement)
        return {row[0]: int(row[1]) for row in result.all()}

    # -----------------------------------------------------------------------
    # Agents
    # -----------------------------------------------------------------------

    async def agent_rows(
        self, start: datetime, end: datetime, *, limit: int, offset: int
    ) -> Sequence[tuple[uuid.UUID | None, str | None, int, int, int, int | None]]:
        """§30's "agent workload", busiest first.

        Returns `(agent_id, agent_name, assigned, open, resolved, average_resolution_seconds)`.

        **A `LEFT` join, so the unassigned bucket is a row.** "How much work has nobody
        picked up" is the first question a manager asks of this screen, and an inner join
        would silently drop it — the workload numbers would then sum to less than the
        organization's ticket count with nothing on screen to explain the gap.

        `User.organization_id` is in the join condition as well as the tenant predicate in
        the `WHERE`, so the join cannot reach a user in another organization. It could not
        today — `assigned_agent_id` is a foreign key and assignment validates the agent —
        but a join that is tenant-safe by inheritance is one refactor away from not being.

        The average is over tickets resolved in the window and measured from `created_at`,
        the same interval and the same start point as the overview's resolution average.
        Grouping it by agent is the only difference between the two.
        """
        assigned = func.count()
        open_count = func.count().filter(Ticket.status.not_in(TERMINAL_STATUSES))
        resolved_count = func.count().filter(Ticket.status.in_(TERMINAL_STATUSES))
        average = func.avg(func.extract("epoch", Ticket.resolved_at - Ticket.created_at)).filter(
            Ticket.resolved_at.is_not(None)
        )

        statement = (
            self._scoped(*self._in_window(start, end))
            .outerjoin(
                User,
                and_(
                    User.id == Ticket.assigned_agent_id,
                    User.organization_id == self.organization_id,
                ),
            )
            .with_only_columns(
                Ticket.assigned_agent_id,
                User.name,
                assigned,
                open_count,
                resolved_count,
                average,
            )
            .group_by(Ticket.assigned_agent_id, User.name)
            .order_by(assigned.desc(), Ticket.assigned_agent_id.asc().nulls_last())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(statement)
        return [
            (
                row[0],
                row[1],
                int(row[2]),
                int(row[3]),
                int(row[4]),
                int(row[5]) if row[5] is not None else None,
            )
            for row in result.all()
        ]

    async def agent_row_count(self, start: datetime, end: datetime) -> int:
        """How many rows `agent_rows` would return unpaged.

        Distinct assignees plus the unassigned bucket if anything is unassigned, which is
        what `agent_rows` groups to. Two numbers in one statement because they are two
        halves of one question.
        """
        unassigned = Ticket.assigned_agent_id.is_(None)
        statement = self._scoped(*self._in_window(start, end)).with_only_columns(
            func.count(distinct(Ticket.assigned_agent_id)),
            func.count().filter(unassigned),
        )
        row = (await self.session.execute(statement)).one()
        return int(row[0]) + (1 if int(row[1]) else 0)

    # -----------------------------------------------------------------------
    # AI usage
    # -----------------------------------------------------------------------

    async def ai_usage(self, start: datetime, end: datetime) -> Mapping[str, Any]:
        """§28's "AI usage": totals, and the same broken down by operation.

        **Empty until Phase T**, because nothing writes `ai_usage` yet — so every number
        here is what a real `COUNT` and `SUM` over the rows that exist actually say.
        §8's eleventh criterion is that analytics come from real aggregation queries and
        never hardcoded values, and a query whose answer is zero is still a query.

        **Row scope is applied through the ticket, not on this table.** `ai_usage` has no
        `customer_id` and no `assigned_agent_id` — it is a ledger of calls, and a call's
        audience is the ticket it was made for. So an agent's AI usage is the usage
        attributed to *their* tickets, expressed as a subquery over `_scoped`, which keeps
        the scope rule in the one place that owns it. Ingestion calls have no ticket and
        are therefore outside every row-scoped view — for an agent, correctly so.

        `cost_usd` is `Numeric`, so a `SUM` comes back as a `Decimal` and is kept as one.
        Six decimal places of a fraction of a cent is exactly the quantity floating point
        would start losing once the sums got large.
        """
        ticket_ids = self._scoped().with_only_columns(Ticket.id)
        window = [AIUsage.created_at >= start, AIUsage.created_at < end]
        attributed = AIUsage.ticket_id.in_(ticket_ids)

        totals = select(
            func.count(),
            func.count().filter(AIUsage.was_successful.is_(False)),
            func.coalesce(func.sum(AIUsage.prompt_tokens), 0),
            func.coalesce(func.sum(AIUsage.completion_tokens), 0),
            func.coalesce(func.sum(AIUsage.cost_usd), 0),
        ).where(AIUsage.organization_id == self.organization_id, *window, attributed)
        row = (await self.session.execute(totals)).one()

        by_operation = select(
            AIUsage.operation,
            func.count(),
            func.coalesce(func.sum(AIUsage.cost_usd), 0),
        ).where(
            AIUsage.organization_id == self.organization_id,
            *window,
            attributed,
        )
        by_operation = by_operation.group_by(AIUsage.operation).order_by(AIUsage.operation)
        rows = (await self.session.execute(by_operation)).all()

        return {
            "calls": int(row[0]),
            "failed_calls": int(row[1]),
            "prompt_tokens": int(row[2]),
            "completion_tokens": int(row[3]),
            "cost_usd": Decimal(row[4]),
            "by_operation": [
                {"operation": operation, "calls": int(calls), "cost_usd": Decimal(cost)}
                for operation, calls, cost in rows
            ],
        }

    # -----------------------------------------------------------------------
    # SLA — the fragment's three uses
    # -----------------------------------------------------------------------

    def _sla_select(self, *criteria: ColumnElement[bool]) -> Select[Any]:
        """A scoped statement joined to the ticket's active policy."""
        return self._scoped(*criteria).join(SLAPolicy, and_(*_POLICY_JOIN))

    async def sla_compliance(self, start: datetime, end: datetime) -> Mapping[str, tuple[int, int]]:
        """Met and missed counts for both timers, over tickets created in the window.

        **The window is the cohort, not the completion date.** A ticket counts here when it
        was *created* in the window and has since been answered or resolved, so every panel
        on one dashboard describes the same set of tickets. Measuring "resolved in the
        window" instead would make the compliance rate and the volume chart answer about
        different populations, and a reader comparing them would be comparing nothing.

        This is `resolve_timer`'s first branch: `stopped_at <= due_at` is `MET` and
        `stopped_at > due_at` is `BREACHED`. `<=` and not `<`, because the deadline instant
        is a moment you are still on time until it has passed.

        One statement for four numbers, because `FILTER` lets the same scan answer all of
        them. Both `FILTER` clauses carry `IS NOT NULL` — an unstopped timer is neither met
        nor missed, and it must not be counted as either.
        """
        response_due = _due_at(SLAPolicy.response_time_minutes)
        resolution_due = _due_at(SLAPolicy.resolution_time_minutes)
        response = Ticket.first_response_at
        resolved = Ticket.resolved_at

        statement = self._sla_select(*self._in_window(start, end)).with_only_columns(
            func.count().filter(response.is_not(None), response <= response_due),
            func.count().filter(response.is_not(None), response > response_due),
            func.count().filter(resolved.is_not(None), resolved <= resolution_due),
            func.count().filter(resolved.is_not(None), resolved > resolution_due),
        )
        row = (await self.session.execute(statement)).one()
        return {"response": (int(row[0]), int(row[1])), "resolution": (int(row[2]), int(row[3]))}

    def _outstanding_predicate(self) -> ColumnElement[datetime | None]:
        """The soonest deadline this ticket has not met, or NULL if it has met both.

        `LEAST` ignores NULL arguments, so wrapping each timer's deadline in
        `_unstopped_due_at` is what turns it into "the soonest outstanding deadline"
        instead of "the soonest deadline, met or not". A ticket with both timers stopped
        produces NULL, and `NULL < now` is unknown rather than true — so it is excluded,
        which is the answer wanted and not an accident: this method exists to find tickets
        with a deadline still ahead of them.
        """
        return func.least(
            _unstopped_due_at(Ticket.first_response_at, SLAPolicy.response_time_minutes),
            _unstopped_due_at(Ticket.resolved_at, SLAPolicy.resolution_time_minutes),
        )

    async def overdue_count(self, now: datetime) -> int:
        """Non-terminal tickets whose nearest outstanding deadline has reached the clock.

        **Not windowed.** This describes the queue as it stands — a ticket created before
        the window and still past due is exactly the one a manager needs to see, and hiding
        it because it fell outside a date range would be the worst possible omission. The
        `range` on the response covers the compliance block; the schema says so.

        Terminal tickets are excluded because `resolve_timer` would report their timers as
        stopped, and "resolved late" is a compliance fact rather than a standing risk — it
        is already counted above.

        **`<=` and not `<`, which the differential test is what settled.**
        `resolve_timer`'s second branch is `now >= due_at` — the deadline instant is already
        a breach, "because at exactly `due_at` there is no time left to act in". A strict
        comparison here would put a ticket that the countdown calls breached one instant
        short of counting as overdue, so the number and the countdown beside it would
        disagree at exactly the instant they matter most. `tests/integration/
        test_analytics_sla_agreement.py` builds a ticket created precisely `target`
        seconds ago and asserts the two agree.
        """
        statement = (
            self._sla_select(Ticket.status.not_in(TERMINAL_STATUSES))
            .with_only_columns(func.count())
            .where(self._outstanding_predicate() <= now)
        )
        return int(await self.session.scalar(statement) or 0)

    async def open_ticket_count(self) -> int:
        """Non-terminal tickets — the denominator the overdue count is out of."""
        return await self._count(self._scoped(Ticket.status.not_in(TERMINAL_STATUSES)))

    async def risk_candidates(self, *, limit: int) -> Sequence[Ticket]:
        """The open tickets nearest a deadline, worst first.

        Ordered by the outstanding-deadline expression ascending, which **is** "least time
        remaining first" — `remaining_seconds` is `due_at - now` and `now` is the same for
        every row, so ordering by the deadline orders by the remaining time without a clock
        comparison anywhere in the query.

        Non-terminal only, joined to an active policy: a ticket whose priority has no active
        policy has no clock, and `resolve_position` is not defined for it. It is absent
        rather than sorted last, which is the same answer `sla_service.decorate` gives.

        `Ticket.id` breaks ties so two tickets sharing a deadline do not swap places between
        two requests for the same view.

        Returns rows, not numbers. The caller computes each one's position with
        `resolve_position` — this method has decided *which* tickets are at risk and in what
        order, and none of the values reported about them.
        """
        statement = (
            self._sla_select(Ticket.status.not_in(TERMINAL_STATUSES))
            .order_by(self._outstanding_predicate().asc().nulls_last(), Ticket.id)
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())
