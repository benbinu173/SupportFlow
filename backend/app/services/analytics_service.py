"""Analytics — the five routes' worth of orchestration, and the one place a clock runs.

Three jobs, in the order they matter:

**1. It resolves the window, once, for five routes.** `start` and `end` are optional on
every endpoint, and every endpoint has to answer the same question about what a missing one
means. `resolve_window` is that answer, and it refuses the two requests that cannot be
answered honestly rather than guessing: a range longer than a year, and a `start` after its
`end`. Both are `422`s with a sentence, following `TicketRepository.list_tickets`'s
convention rather than truncating a window a caller asked for — a silently shortened range
makes a chart lie about its own x-axis.

**2. It computes the risk list with `sla_service`, and computes no clock itself.** §31's
"SLA risks" is the reason Phase Q left a promise in `app/api/sla.py`'s docstring, and this
module is where that promise is kept: `sla_service.load_policies` reads the tenant's
targets and `sla_service.resolve_position` produces every number on every row. The
repository decided *which* tickets are on the list; this module decides *what is true about
them*.

**3. It composes the cached half of a response with the live half.** `/analytics/sla` is the
one route where those differ — compliance is an aggregate over the tenant's whole set and is
cached, while `remaining_seconds` is a function of `now` and a cached countdown would be a
wrong countdown. §15's instruction is *"do not cache everything blindly"*, and this is the
distinction it is asking for: the two halves are built separately because they have
different expiry semantics, not because they were easier that way.

**The cached payload is the response model itself**, so `range` comes back out of Redis
along with the numbers it describes. A cache that stored bare numbers and rebuilt the
envelope would be a second place that knows what a response looks like.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cache
from app.core.config import get_settings
from app.core.exceptions import ValidationError
from app.core.tenancy import TenantContext
from app.models.enums import Sentiment, TicketPriority, TicketStatus
from app.repositories.analytics_repository import AnalyticsRepository
from app.repositories.sla_repository import TERMINAL_STATUSES
from app.schemas.analytics import (
    AgentPerformanceRow,
    AIUsageSummary,
    AnalyticsAgents,
    AnalyticsOverview,
    AnalyticsRange,
    AnalyticsRiskTicket,
    AnalyticsSentiment,
    AnalyticsSLA,
    AnalyticsTickets,
    CategoryCount,
    DurationSummary,
    PriorityCount,
    SentimentBucket,
    SLAComplianceSummary,
    SLAStanding,
    SLATimerCompliance,
    TicketTotals,
    VolumePoint,
)
from app.schemas.sla import SLATimer
from app.services import sla_service
from app.services.sla_service import SLAPosition, TimerPosition

#: The window a request gets when it names neither end. Thirty days is the shortest span
#: that shows a month-over-month shape without a chart that needs scrolling, and it is the
#: default the README documents.
DEFAULT_WINDOW_DAYS = 30

#: The longest window that may be asked for. A ceiling and not a truncation: a caller that
#: asked for five years and silently got one would be reading a total that excluded 80% of
#: its tickets with nothing on the response to say so.
MAX_WINDOW_DAYS = 366


def resolve_window(
    start: datetime | None, end: datetime | None, *, now: datetime
) -> tuple[datetime, datetime]:
    """The window a request describes, defaulted, validated, and timezone-aware.

    `end` defaults to `now` and `start` to `now - 30 days`, so a bare
    `GET /analytics/overview` answers "the last month" rather than refusing. The default is
    echoed back in `AnalyticsRange`, which is the whole reason that field exists: a client
    that sent nothing learns what it actually got.

    **A naive datetime is refused, not interpreted.** Every timestamp in the database is
    `timestamptz`, so a naive bound would be compared against an aware column — and psycopg
    does not raise on that, it applies the session's timezone and returns an answer that is
    silently shifted by hours. The client would see a wrong number with nothing to explain
    it, which is the failure `resolve_timer`'s docstring calls out and refuses to have.
    A `422` naming the missing offset is the honest version of the same refusal.
    """
    if start is None:
        start = now - timedelta(days=DEFAULT_WINDOW_DAYS)
    if end is None:
        end = now

    for name, value in (("start", start), ("end", end)):
        if value.tzinfo is None:
            raise ValidationError(
                f"'{name}' must include a timezone offset, for example "
                "2026-01-01T00:00:00Z. Every stored timestamp is timezone-aware, so a "
                "naive bound would be shifted by the server's timezone rather than "
                "compared against the value you meant."
            )

    if start > end:
        raise ValidationError("'start' must not be later than 'end'.")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise ValidationError(
            f"A range cannot be longer than {MAX_WINDOW_DAYS} days. Narrow the window "
            "rather than having it truncated: a shortened range would report a total for "
            "a period you did not ask about."
        )

    return start, end


def _ttl() -> int:
    """The configured cache lifetime. One setting, read here, applied to every entry."""
    return get_settings().ANALYTICS_CACHE_TTL_SECONDS


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------


def _totals(counts: Mapping[TicketStatus, int]) -> TicketTotals:
    """A complete status breakdown, and the four numbers that follow from it.

    **Every status is present, including the ones with no tickets.** `GROUP BY` only returns
    the values that occur, so the missing members are filled in here — a chart with a fixed
    set of series must not gain a series the first time somebody closes a ticket.

    `total` and `open` are derived from `by_status` rather than counted separately, which is
    what makes them a summary of the breakdown instead of a second opinion about it. Two
    queries could disagree; one dict cannot.
    """
    by_status = {status: counts.get(status, 0) for status in TicketStatus}
    total = sum(by_status.values())
    terminal = sum(by_status[status] for status in TERMINAL_STATUSES)

    return TicketTotals(
        total=total,
        open=total - terminal,
        resolved=by_status[TicketStatus.RESOLVED],
        closed=by_status[TicketStatus.CLOSED],
        by_status=by_status,
    )


def _duration(average_and_count: tuple[int, int]) -> DurationSummary:
    """An average and its row count, with "no rows" rendered as `null`.

    `average_seconds` is `None` rather than `0` when nothing is behind it. An average of no
    rows does not exist, and zero would put "answered instantly" on a dashboard for a
    tenant that has answered nothing.
    """
    average, count = average_and_count
    return DurationSummary(average_seconds=average if count else None, count=count)


def _timer_compliance(timer: SLATimer, met: int, breached: int) -> SLATimerCompliance:
    """One timer's record, with the rate derived rather than counted.

    `rate` is `None` with nothing stopped, so "no data" cannot be mistaken for a perfect
    record — the same distinction `DurationSummary` makes, and the reason these are
    nullable rather than defaulted to `1.0`.
    """
    stopped = met + breached
    return SLATimerCompliance(
        timer=timer,
        met=met,
        breached=breached,
        rate=(met / stopped) if stopped else None,
    )


def _compliance(counts: Mapping[str, tuple[int, int]]) -> SLAComplianceSummary:
    """Both timers' records, and the two of them pooled.

    Pooled as a sum of the two timers' counts rather than as an average of their rates: a
    tenant with 100 resolutions and 2 responses has a pooled rate near the resolution
    figure, not the midpoint of the two. `met + breached` over both is what "how much of
    the work met its target" means.
    """
    response = _timer_compliance(SLATimer.RESPONSE, *counts["response"])
    resolution = _timer_compliance(SLATimer.RESOLUTION, *counts["resolution"])

    met = response.met + resolution.met
    breached = response.breached + resolution.breached
    stopped = met + breached

    return SLAComplianceSummary(
        response=response,
        resolution=resolution,
        met=met,
        breached=breached,
        rate=(met / stopped) if stopped else None,
    )


# ---------------------------------------------------------------------------
# The five reads
# ---------------------------------------------------------------------------


async def overview(
    session: AsyncSession,
    context: TenantContext,
    *,
    start: datetime,
    end: datetime,
) -> AnalyticsOverview:
    """§28's headline block. Cached — every number on it is an aggregate over a window.

    No `now`: unlike `/analytics/sla` this response contains no countdown, so the only
    instant it depends on is the one the window was resolved against — which the caller
    has already turned into `start` and `end`.
    """
    key = await cache.key_for("overview", context, {"start": start, "end": end})
    return await cache.read_through(
        key,
        model=AnalyticsOverview,
        ttl=_ttl(),
        produce=lambda: _overview(session, context, start=start, end=end),
    )


async def _overview(
    session: AsyncSession, context: TenantContext, *, start: datetime, end: datetime
) -> AnalyticsOverview:
    """The uncached computation. Six aggregates, four of them one statement each.

    Every one goes through `AnalyticsRepository`, so every one carries the tenant predicate
    and the caller's row scope. An agent reading this gets their own numbers and a manager
    reading it gets the organization's — the same endpoint, the same SQL, one scope
    predicate in between.
    """
    repository = AnalyticsRepository(session, context)
    averages = await repository.duration_averages(start, end)
    ai_usage = await repository.ai_usage(start, end)

    return AnalyticsOverview(
        range=AnalyticsRange(start=start, end=end),
        totals=_totals(await repository.status_counts(start, end)),
        volume=[
            VolumePoint(bucket=bucket, count=count)
            for bucket, count in await repository.volume(start, end)
        ],
        response_time=_duration(averages["response"]),
        resolution_time=_duration(averages["resolution"]),
        ai_usage=AIUsageSummary.model_validate(ai_usage),
    )


async def tickets(
    session: AsyncSession, context: TenantContext, *, start: datetime, end: datetime
) -> AnalyticsTickets:
    """§28's "tickets by priority" and "tickets by category". **Not cached.**

    The two grouped queries the indexes were built for — `ix_tickets_org_status_priority`
    and `ix_tickets_org_category` — each returning at most a handful of rows over an
    organization's tickets. Caching them would buy a `GET` and a `SET` in exchange for the
    chance of showing a stale count on the one chart a manager refreshes after assigning
    work, which is a trade with nothing on the good side of it.

    `by_priority` is complete across the enum; `by_category` is a top-N, because categories
    are free text written by Phase U and there is no fixed set to be complete over.
    """
    repository = AnalyticsRepository(session, context)
    priorities = await repository.priority_counts(start, end)

    return AnalyticsTickets(
        range=AnalyticsRange(start=start, end=end),
        by_priority=[
            PriorityCount(priority=priority, count=priorities.get(priority, 0))
            for priority in TicketPriority
        ],
        by_category=[
            CategoryCount(category=category, count=count)
            for category, count in await repository.category_counts(start, end, limit=20)
        ],
    )


async def agents(
    session: AsyncSession,
    context: TenantContext,
    *,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> AnalyticsAgents:
    """§30's "agent workload". Cached, and **the only route an agent cannot reach**.

    A per-agent breakdown is a comparison between people, which is §3's "org-wide
    analytics" and requires `ANALYTICS_ORG`. An agent's own performance is what the other
    four routes return for them — which they can read, and which is why this endpoint's
    capability differs from theirs rather than the other way round.
    """
    key = await cache.key_for(
        "agents", context, {"start": start, "end": end, "limit": limit, "offset": offset}
    )
    return await cache.read_through(
        key,
        model=AnalyticsAgents,
        ttl=_ttl(),
        produce=lambda: _agents(session, context, start=start, end=end, limit=limit, offset=offset),
    )


async def _agents(
    session: AsyncSession,
    context: TenantContext,
    *,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> AnalyticsAgents:
    repository = AnalyticsRepository(session, context)
    rows = await repository.agent_rows(start, end, limit=limit, offset=offset)

    return AnalyticsAgents(
        range=AnalyticsRange(start=start, end=end),
        agents=[
            AgentPerformanceRow(
                agent_id=agent_id,
                agent_name=name,
                assigned=assigned,
                open=open_count,
                resolved=resolved,
                average_resolution_seconds=average,
            )
            for agent_id, name, assigned, open_count, resolved, average in rows
        ],
        total=await repository.agent_row_count(start, end),
    )


async def sentiment(
    session: AsyncSession, context: TenantContext, *, start: datetime, end: datetime
) -> AnalyticsSentiment:
    """§28's "sentiment distribution". **Not cached.**

    One grouped query over a window, and the cheapest of the five. It is also the one whose
    answer a reader is most likely to refresh while watching Phase U fill the column in, so
    a cached copy would be the most noticeably stale entry in the API.

    **Empty until Phase U**, and that reads as one bucket with every ticket under it: the
    `None` key is "not analysed yet", which is the true answer rather than a placeholder.
    """
    counts = await AnalyticsRepository(session, context).sentiment_counts(start, end)
    unanalysed = counts.get(None, 0)

    return AnalyticsSentiment(
        range=AnalyticsRange(start=start, end=end),
        buckets=[
            SentimentBucket(sentiment=None, count=unanalysed),
            *[SentimentBucket(sentiment=value, count=counts.get(value, 0)) for value in Sentiment],
        ],
        analysed=sum(counts.get(value, 0) for value in Sentiment),
        unanalysed=unanalysed,
    )


async def sla(
    session: AsyncSession,
    context: TenantContext,
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    limit: int,
) -> AnalyticsSLA:
    """§28's `GET /analytics/sla` and §31's "SLA risks", in one response.

    **Two halves, built differently on purpose.** The standing — compliance over the window,
    how many open tickets are past due, how many are open at all — is cached, because it is
    an aggregate and it changes only when a ticket changes. The risk list is not, because
    `remaining_seconds` is a countdown measured from `now`: a cached one would tell a
    manager that a ticket has forty minutes left when it breached an hour ago. Serving a
    stale countdown is worse than serving no countdown, because it is actionable and wrong.

    Note which of the two counts is windowed and which is not. `compliance` describes
    tickets *created* in the window; `overdue` and `open_tickets` describe the queue as it
    stands right now, whatever `start` says. A ticket raised last month and still past due
    is the one a manager most needs to see, and it would be an odd dashboard that hid it
    for falling outside a date range. `AnalyticsRange` is the compliance block's window and
    the schema says so.
    """
    standing = await _standing(session, context, start=start, end=end)
    risks = await risk_list(session, context, now=now, limit=limit)

    return AnalyticsSLA(
        range=AnalyticsRange(start=start, end=end),
        compliance=standing.compliance,
        overdue=standing.overdue,
        open_tickets=standing.open_tickets,
        risks=risks,
    )


async def _standing(
    session: AsyncSession, context: TenantContext, *, start: datetime, end: datetime
) -> SLAStanding:
    """The cacheable half of `/analytics/sla`.

    One cached entry per tenant and window, holding three numbers and a rate — so a
    dashboard that polls every thirty seconds runs two `COUNT`s and a `GROUP BY` a minute
    rather than on every poll, and the risk list it is actually watching stays live.
    """
    key = await cache.key_for("sla-standing", context, {"start": start, "end": end})
    return await cache.read_through(
        key,
        model=SLAStanding,
        ttl=_ttl(),
        produce=lambda: _standing_uncached(session, context, start=start, end=end),
    )


async def _standing_uncached(
    session: AsyncSession, context: TenantContext, *, start: datetime, end: datetime
) -> SLAStanding:
    repository = AnalyticsRepository(session, context)

    return SLAStanding(
        compliance=_compliance(await repository.sla_compliance(start, end)),
        overdue=await repository.overdue_count(datetime.now(UTC)),
        open_tickets=await repository.open_ticket_count(),
    )


async def risk_list(
    session: AsyncSession,
    context: TenantContext,
    *,
    now: datetime,
    limit: int,
) -> list[AnalyticsRiskTicket]:
    """§31's "SLA risks": the open tickets nearest a deadline, worst first. **Never cached.**

    The three steps, and the boundary between them is the phase's central decision:

    1. `risk_candidates` selects and orders in SQL, using the one `due_at` fragment.
    2. `sla_service.load_policies` reads the tenant's targets — four rows, once.
    3. `sla_service.resolve_position` produces every value on every row.

    So the SQL decides **which** tickets and in **what order**, and the clock decides
    **what is true about them**. A disagreement between the two therefore costs a wrong
    position in a list and never a wrong number on a screen;
    `tests/integration/test_analytics_sla_agreement.py` asserts they agree row for row, at
    every boundary, in both directions.

    **The order is the SQL's and is not re-sorted here.** Re-sorting by the clock's own
    `remaining_seconds` would look like a safety net and would actually destroy the
    property that makes `limit` mean anything: the ten tickets the query selected are the
    ten it ranked, and a Python sort over them cannot recover an eleventh that was excluded
    for ranking seventeenth. The differential test asserts the two orders agree instead,
    which catches a wrong comparison operator in the fragment — the thing a re-sort would
    paper over.

    Two `continue`s are belt to SQL's braces. A ticket whose priority has no active policy
    has no clock at all, and one with both timers stopped has no outstanding deadline; the
    query already excludes both, and skipping them here means a change to the query could
    not put a `None` into a response field typed as a `datetime`.
    """
    tickets = await AnalyticsRepository(session, context).risk_candidates(limit=limit)
    if not tickets:
        return []

    policies = await sla_service.load_policies(session, context)
    risks: list[AnalyticsRiskTicket] = []

    for ticket in tickets:
        policy = policies.get(ticket.priority)
        if policy is None:
            continue
        binding = _binding_timer(sla_service.resolve_position(ticket, policy, now=now))
        if binding is None:
            continue

        risks.append(
            AnalyticsRiskTicket(
                id=ticket.id,
                number=ticket.number,
                subject=ticket.subject,
                status=ticket.status,
                priority=ticket.priority,
                assigned_agent_id=ticket.assigned_agent_id,
                timer=binding.timer,
                state=binding.state,
                due_at=binding.due_at,
                remaining_seconds=binding.remaining_seconds,
            )
        )

    return risks


def _binding_timer(position: SLAPosition) -> TimerPosition | None:
    """The timer that puts this ticket on the risk list: the soonest one still running.

    `min` over `remaining_seconds`, which for an unstopped timer is `due_at - now` — the
    same `now` for both, so the smallest remaining time is the earliest deadline. That is
    the clock's own number and not a second ranking rule: the SQL fragment orders by the
    same quantity computed in the database, and this picks which of a ticket's two timers
    that quantity belongs to.

    `None` when neither timer is running. A ticket in that state has met both deadlines and
    is not a risk anything; the query excludes it, and this is the type-level reason it has
    to.
    """
    running = [
        timer for timer in (position.response, position.resolution) if timer.stopped_at is None
    ]
    if not running:
        return None
    return min(running, key=lambda timer: timer.remaining_seconds)


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "agents",
    "overview",
    "resolve_window",
    "risk_list",
    "sentiment",
    "sla",
    "tickets",
]
