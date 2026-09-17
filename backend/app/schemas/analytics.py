"""Analytics vocabulary — what the five read-only endpoints answer with.

Spec §6 names this module, and §28 names the metrics it carries. Three conventions are
borrowed from modules that already settled them, rather than re-decided here:

* **Seconds, not milliseconds**, for every duration. `SLATimerRead.remaining_seconds` set
  the unit for a time-to-deadline, and a second unit for a slightly different duration
  would be the kind of inconsistency a client discovers by rendering "0.02 hours".
* **Half-open windows** — `start` inclusive, `end` exclusive — for exactly the reason
  `TicketRepository.list_tickets` records: adjacent windows have to tile without a row
  appearing in both, and a `timestamptz` boundary belongs to one window.
* **Enum-keyed breakdowns are complete.** Every member of `TicketStatus`, `TicketPriority`,
  and `Sentiment` appears in its breakdown, including the ones whose count is zero, so a
  chart is a stable set of series rather than one that grows when a status is first used.
  Category is the exception and is deliberately not an enum: it is free text written by AI
  classification (Phase U), so its breakdown is a top-N list with an `None` bucket for
  "not classified yet".

**Nothing here computes anything**, for the reason `app/schemas/sla.py` gives about itself:
a schema that could derive a deadline would be a second implementation of the clock. The
numbers below are produced by `app/repositories/analytics_repository.py` and
`app/services/analytics_service.py`.

**`cost_usd` is a `Decimal`**, not a `float`, for the reason
`app/models/ai_usage.py` already gives about its own column: it is money and it gets
summed. It renders as a JSON string, which is what keeps six decimal places six decimal
places.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.models.enums import AIOperation, Sentiment, TicketPriority, TicketStatus
from app.schemas.sla import SLATimer, SLATimerState

# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


class AnalyticsRange(BaseModel):
    """The window the numbers describe, echoed back.

    Returned rather than assumed, so a client that sent no parameters learns what it
    actually got. A response whose meaning depends on a default the client cannot see is
    a response that will be misread exactly once, in a screenshot.
    """

    start: datetime
    end: datetime
    # Always `day`. The range ceiling is what bounds the number of points, and a second
    # bucket size would mean a second `date_trunc` expression and a rule about when to
    # pick it — for a chart whose x-axis gets crowded well before the ceiling is reached.
    #
    # **Buckets are UTC days.** Nothing in the schema carries an organization's timezone,
    # so a local-midnight bucket is not available; the README records that rather than
    # this module inventing a setting nothing sets.
    bucket: Literal["day"] = "day"


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


class TicketTotals(BaseModel):
    """§28's first four metrics, plus the breakdown that makes them checkable."""

    total: int
    open: int
    resolved: int
    closed: int
    # Every member of the enum, including zeros. `total` equals their sum, which is the
    # property that makes the four numbers above it a summary rather than a claim.
    by_status: dict[TicketStatus, int]


class VolumePoint(BaseModel):
    """One bucket of §28's "ticket volume over time"."""

    bucket: datetime
    count: int


class DurationSummary(BaseModel):
    """An average elapsed time, with the number of rows behind it.

    **`average_seconds` is `None`, not `0`, when nothing is behind it.** An average of no
    rows does not exist, and rendering it as zero puts "resolved instantly" on a dashboard
    for a tenant that has resolved nothing — which is the same reasoning that makes
    `TicketSLARead` absent rather than fabricated when there is no policy.
    """

    average_seconds: int | None
    count: int


class PriorityCount(BaseModel):
    priority: TicketPriority
    count: int


class CategoryCount(BaseModel):
    """One category's volume. `category` is `None` for tickets not yet classified."""

    category: str | None
    count: int


class SentimentBucket(BaseModel):
    """One sentiment's volume. `sentiment` is `None` for tickets not yet analysed."""

    sentiment: Sentiment | None
    count: int


class AgentPerformanceRow(BaseModel):
    """§30's "agent workload", one agent per row.

    `agent_id` is `None` for the unassigned bucket, which is a row rather than an
    omission: "how much work has nobody picked up" is a manager's first question and it
    has no assignee to be filed under.
    """

    agent_id: uuid.UUID | None
    agent_name: str | None
    assigned: int
    open: int
    resolved: int
    # Over this agent's resolved tickets in the window, measured from `created_at` to
    # `resolved_at` — the same interval as the overview's resolution average, grouped.
    average_resolution_seconds: int | None


# ---------------------------------------------------------------------------
# SLA
# ---------------------------------------------------------------------------


class SLATimerCompliance(BaseModel):
    """One timer's record over a window: how often the deadline was met.

    `rate` is `None` when no timer stopped in the window, following `DurationSummary`.
    Every row counted here has a `stopped_at`, so "no data" and "nothing was late" cannot
    be confused with a fabricated rate of 1.0.
    """

    timer: SLATimer
    met: int
    breached: int
    # A float in [0, 1], not a percentage: the client formats it, and a rate that changes
    # meaning depending on whether somebody multiplied by 100 is a rate that will be
    # displayed wrong once.
    rate: float | None


class SLAComplianceSummary(BaseModel):
    """Both timers, and the two of them together — §28's "SLA compliance"."""

    response: SLATimerCompliance
    resolution: SLATimerCompliance
    met: int
    breached: int
    rate: float | None


class AnalyticsRiskTicket(BaseModel):
    """One ticket in §31's "SLA risks", ranked by the deadline that is nearest.

    **Every field except `subject` comes from `sla_service.resolve_position`** — the same
    call the ticket detail screen makes, so the countdown here and the countdown there
    cannot disagree. The repository's SQL decides which tickets are on the list and in
    what order; it decides none of the numbers.
    """

    id: uuid.UUID
    number: int
    subject: str
    status: TicketStatus
    priority: TicketPriority
    assigned_agent_id: uuid.UUID | None

    # Which timer is nearest, and where it stands. `timer` is the binding one of the two —
    # the soonest deadline that has not been met.
    timer: SLATimer
    state: SLATimerState
    due_at: datetime
    remaining_seconds: int


class SLAStanding(BaseModel):
    """The half of `/analytics/sla` that is an aggregate rather than a countdown.

    A model of its own because it is the unit that gets cached, and the cache stores the
    response object it is handed. Caching an `AnalyticsSLA` with its `risks` empty and
    patching the list in afterwards would work and would be a lie about what the entry
    holds — an entry that says it is a response but is not one is the kind of thing that
    looks fine until something re-reads it.

    None of these three numbers depends on `now` in any interesting way: compliance is over
    stopped timers, `overdue` is a comparison of deadlines already past, and `open_tickets`
    is a status count. That is exactly what qualifies them to be shared across requests
    for a few minutes, and what disqualifies `risks` from joining them.
    """

    compliance: SLAComplianceSummary
    overdue: int
    open_tickets: int


class AnalyticsSLA(BaseModel):
    """§28's `GET /analytics/sla` and §31's "SLA risks" in one response.

    **The two halves are built differently on purpose.** `compliance` and the two counts
    are aggregates over the whole scoped set and are cached; `risks` is computed per
    request because `remaining_seconds` is a function of `now` and a cached countdown
    would be a wrong countdown. §15's "do not cache everything blindly" is asking for
    exactly this distinction.

    They also answer about different populations, which is worth knowing before comparing
    them: `compliance` describes tickets **created in `range`**, while `overdue` and
    `open_tickets` describe the queue **as it stands now**, whatever the window says. A
    ticket raised before the window and still past due appears in the second pair and not
    the first, which is the reading a manager wants — the alternative hides the worst
    tickets because they are old.
    """

    range: AnalyticsRange
    compliance: SLAComplianceSummary
    # Non-terminal tickets whose nearest unstopped deadline is already in the past. Counted
    # in SQL from the due instant alone — see `analytics_repository` on why the warning
    # band is deliberately not re-expressed there.
    overdue: int
    # The denominator the number above is out of, so "3 overdue" can be read as alarming or
    # not without a second request.
    open_tickets: int
    # A ranking, not a page. `limit` defaults to 10 and caps at 50; there is no `offset`,
    # because the eleventh-worst ticket is not something a dashboard shows. The order is
    # the query's — see `analytics_service.risk_list` on why it is not re-sorted here.
    risks: list[AnalyticsRiskTicket]


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------


class AIUsageOperationRow(BaseModel):
    """§28's "AI usage", broken down by what the calls were for."""

    operation: AIOperation
    calls: int
    cost_usd: Decimal


class AIUsageSummary(BaseModel):
    """§28's "AI usage".

    **Empty until Phase T, and that is a real answer rather than a placeholder.** Nothing
    writes `ai_usage` yet, so every field here is zero because a `COUNT` and a `SUM` over
    the rows that exist say so. §8's eleventh criterion is that analytics come from real
    aggregation queries and never hardcoded values; this is a real query, and the honest
    number for a tenant that has made no AI calls is zero.

    `failed_calls` is separate from `calls` because `ai_usage` records a failed call — it
    consumed quota and may have been billed. A dashboard that folded them together would
    report spend with no way to see how much of it bought nothing.
    """

    calls: int
    failed_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    by_operation: list[AIUsageOperationRow]


# ---------------------------------------------------------------------------
# The five responses
# ---------------------------------------------------------------------------


class AnalyticsOverview(BaseModel):
    """§28's headline block: volume, status, both averages, and AI spend."""

    range: AnalyticsRange
    totals: TicketTotals
    volume: list[VolumePoint]
    # "Average first response time" and "average resolution time" (§28). Both are measured
    # from `created_at`, which is where both SLA timers start — the same start point
    # `resolve_position` uses, so this average and the SLA percentages describe the same
    # clock rather than two different ones.
    response_time: DurationSummary
    resolution_time: DurationSummary
    ai_usage: AIUsageSummary


class AnalyticsTickets(BaseModel):
    """§28's "tickets by category" and "tickets by priority"."""

    range: AnalyticsRange
    by_priority: list[PriorityCount]
    by_category: list[CategoryCount]


class AnalyticsAgents(BaseModel):
    """§30's "agent workload"."""

    range: AnalyticsRange
    agents: list[AgentPerformanceRow]
    # Rows before paging, so a client can render "showing 10 of 34" — the same reason
    # `AgentPerformanceRow` carries the unassigned bucket rather than hiding it.
    total: int


class AnalyticsSentiment(BaseModel):
    """§28's "sentiment distribution". Filled by Phase U; every ticket is in the `None`
    bucket until then, with the three real sentiments present at zero beside it.
    """

    range: AnalyticsRange
    buckets: list[SentimentBucket]
    analysed: int
    unanalysed: int
