"""Analytics endpoints — five read-only views over a tenant's work.

**Five routes, two capabilities, and the split is §3's matrix rather than a choice made
here.** `ANALYTICS_OWN` guards four of them: it is the capability all three staff roles
hold, and what differs between them is how many rows it reaches — an admin or manager gets
the organization, an agent gets their assigned work. That is `row_scope_for(role,
TICKET_SCOPE_BY_ROLE)`, applied inside every query, so **one endpoint answers differently
by role exactly as `GET /tickets` does** and no second scope map exists to disagree with the
first. `ANALYTICS_ORG` guards `/agents` alone, because a per-agent breakdown is a comparison
between people; an agent's own performance is what the other four routes already return for
them.

**Nothing here computes anything.** The numbers come from `analytics_repository`'s
aggregates, the SLA values from `sla_service.resolve_position`, and the assembled responses
from `analytics_service`. A route translates a query string into a window and hands it on —
which is why none of the five is longer than a few lines.

**Every route reads and none writes**, so there is no `Origin` dependency: nothing here
writes an audit row, and demanding the caller's IP address for a `GET` would be collecting
data no table records.

**The cache is invisible from here.** `/overview`, `/agents`, and half of `/sla` are served
from Redis when they can be, and the responses are byte-identical either way — the cached
value is the same response model, re-validated on the way out. A route does not know, and
must not need to.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import Context, DbSession, require_permission
from app.core.permissions import Permission
from app.schemas.analytics import (
    AnalyticsAgents,
    AnalyticsOverview,
    AnalyticsSentiment,
    AnalyticsSLA,
    AnalyticsTickets,
)
from app.services import analytics_service

router = APIRouter()

# Declared once and attached per route rather than hoisted into a shared list, following
# `app/api/sla.py`: the two capabilities guard different sets of routes, and
# `tests/security/test_route_protection.py` walks the routing table, so a sixth route
# arriving without one is caught rather than trusted.
_own = [Depends(require_permission(Permission.ANALYTICS_OWN))]
_org = [Depends(require_permission(Permission.ANALYTICS_ORG))]

_start = Annotated[
    datetime | None,
    Query(description="Inclusive lower bound. Defaults to 30 days before `end`."),
]
_end = Annotated[
    datetime | None,
    Query(description="Exclusive upper bound. Defaults to now."),
]


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """Resolve the two shared range parameters against **one** instant.

    `now` is read once and used for both bounds, so a request that supplies neither gets a
    window with no gap or overlap at its own edges, and a request that supplies one gets a
    default derived from the same moment it is being answered at. Reading the clock twice
    would be a window a few microseconds wider than the one the caller was told about.

    The validation itself is `analytics_service.resolve_window`'s; this only supplies the
    clock, so the rules live in one place and are unit-testable without a request.
    """
    return analytics_service.resolve_window(start, end, now=datetime.now(UTC))


@router.get(
    "/overview",
    response_model=AnalyticsOverview,
    summary="Ticket volume, status totals, both averages, and AI spend",
    dependencies=_own,
)
async def overview(
    context: Context,
    db: DbSession,
    start: _start = None,
    end: _end = None,
) -> AnalyticsOverview:
    """§28's headline block, cached.

    `totals.by_status` carries every member of `TicketStatus`, including the ones with no
    tickets, and `total` is their sum rather than a separately counted number — so the
    breakdown and the summary cannot disagree.

    `volume` has one point per UTC day that has tickets. **Days with none are absent rather
    than zero**, because filling them in means generating a calendar series in the database
    for every request; the client plotting the chart is the one that knows which days it
    wants on the axis.

    `response_time.average_seconds` and `resolution_time.average_seconds` are `null`, not
    zero, when nothing in the window has been answered or resolved — an average of no rows
    does not exist, and zero would report an instantly-answered desk.

    `ai_usage` is a real `COUNT` and `SUM` over the rows the tenant has, written since
    Phase T by `app/services/ai_service.py` — the one call path. It reads zero for a tenant
    that has made no calls, and §8's eleventh criterion is that analytics come from real
    aggregation queries and never hardcoded values. It is **cached**, unlike `/tickets`:
    it is an aggregate over a window, and the TTL is the trade `app/core/cache.py`
    documents. `calls` counts only calls attributed to a ticket in the caller's row scope,
    so a call made with no ticket is spend without a place on this screen.

    An agent calling this gets their own totals; a manager calling it gets the
    organization's. Same route, same SQL, one row-scope predicate in between.
    """
    start_at, end_at = _window(start, end)
    return await analytics_service.overview(db, context, start=start_at, end=end_at)


@router.get(
    "/tickets",
    response_model=AnalyticsTickets,
    summary="Tickets by priority and by category",
    dependencies=_own,
)
async def tickets(
    context: Context,
    db: DbSession,
    start: _start = None,
    end: _end = None,
) -> AnalyticsTickets:
    """§28's two ticket breakdowns, uncached.

    Both are single grouped queries over indexes Phase D built for them
    (`ix_tickets_org_status_priority`, `ix_tickets_org_category`). Caching them would trade
    a `GET` and a `SET` for the chance of showing a stale count on the one chart somebody
    refreshes the moment they assign work.

    `by_priority` lists every priority in declaration order. `by_category` is a top-20,
    because `category` is free text written by AI classification in Phase U — there is no
    fixed set to be complete over, and until that phase runs every ticket is in the `null`
    bucket, which is the truth about the tenant rather than a placeholder.
    """
    start_at, end_at = _window(start, end)
    return await analytics_service.tickets(db, context, start=start_at, end=end_at)


@router.get(
    "/agents",
    response_model=AnalyticsAgents,
    summary="Per-agent workload and resolution times",
    dependencies=_org,
)
async def agents(
    context: Context,
    db: DbSession,
    start: _start = None,
    end: _end = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AnalyticsAgents:
    """§30's "agent workload", busiest first. Cached. **`ANALYTICS_ORG` only.**

    §3 draws the line here: reading the organization's own numbers is the capability an
    agent holds, and comparing agents against each other is not. An agent calling this gets
    a `403`, and their own figures are on the four routes they can reach.

    The list **includes an unassigned row** — `agent_id` and `agent_name` both `null` — so
    "how much work has nobody picked up" is on the screen rather than missing from a total
    that does not add up. `total` is the number of rows before paging, so a client can
    render "showing 10 of 34" without a second request.

    `average_resolution_seconds` is measured from ticket creation, the same interval the
    overview's resolution average uses; grouping it by agent is the only difference.
    """
    start_at, end_at = _window(start, end)
    return await analytics_service.agents(
        db, context, start=start_at, end=end_at, limit=limit, offset=offset
    )


@router.get(
    "/sla",
    response_model=AnalyticsSLA,
    summary="SLA compliance, and the tickets nearest a deadline",
    dependencies=_own,
)
async def sla(
    context: Context,
    db: DbSession,
    start: _start = None,
    end: _end = None,
    limit: Annotated[int, Query(ge=1, le=50, description="How many at-risk tickets to rank.")] = 10,
) -> AnalyticsSLA:
    """§28's SLA compliance and §31's "SLA risks", in one response.

    **The two halves are built differently, and the difference is the point.** `compliance`,
    `overdue`, and `open_tickets` are aggregates and are cached; `risks` is computed on
    every request because `remaining_seconds` is a countdown measured from now — a cached
    one would confidently tell a manager that a ticket has forty minutes left when it
    breached an hour ago. §15's instruction is "do not cache everything blindly", and this
    is the distinction it is asking for.

    They also describe different populations, which matters when reading them together.
    Compliance is over tickets **created in the window** — the cohort reading, so the rate
    and the volume chart describe the same set of tickets. `overdue` and `open_tickets`
    describe the queue **as it stands**, regardless of `start`: a ticket raised before the
    window and still past due is exactly the one worth surfacing, and hiding it because it
    fell outside a date range would be the worst possible omission.

    `risks` is a **ranking, not a page** — `limit` defaults to 10 and caps at 50, and there
    is no `offset`, because the eleventh-worst ticket is not a dashboard's business. Each
    row's `timer`, `state`, `due_at`, and `remaining_seconds` come from the same
    `resolve_position` call the ticket detail screen makes, so a countdown here and a
    countdown there cannot disagree. The ordering is the query's: see
    `analytics_service.risk_list` for why it is not re-sorted in Python.

    A ticket whose priority has no active policy has no clock and is absent from both
    halves — the same absence `GET /tickets/{id}` reports as a `null` `sla`.
    """
    now = datetime.now(UTC)
    start_at, end_at = analytics_service.resolve_window(start, end, now=now)
    return await analytics_service.sla(
        db, context, start=start_at, end=end_at, now=now, limit=limit
    )


@router.get(
    "/sentiment",
    response_model=AnalyticsSentiment,
    summary="Sentiment distribution across tickets",
    dependencies=_own,
)
async def sentiment(
    context: Context,
    db: DbSession,
    start: _start = None,
    end: _end = None,
) -> AnalyticsSentiment:
    """§28's "sentiment distribution", uncached.

    One grouped query and the cheapest of the five, so there is nothing for a cache to save
    — and it is the view most likely to be refreshed while watching Phase U fill the column
    in, which makes a cached copy the most noticeably stale entry in the API.

    **Empty until Phase U, and it reads as one bucket carrying every ticket.** The `None`
    bucket holds all of them and the three real sentiments sit at zero beside it — a fixed
    set of series, the same completeness `totals.by_status` has. The `None` bucket is named
    rather than omitted so `unanalysed` is a visible number: a distribution that silently
    covered only classified tickets would show a percentage of an undefined whole.
    """
    start_at, end_at = _window(start, end)
    return await analytics_service.sentiment(db, context, start=start_at, end=end_at)
