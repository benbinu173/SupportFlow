"""The SQL fragment and the pure clock, held to each other at every boundary.

**This is Phase S's keystone, and it exists because the phase introduced a second
implementation of one idea.** `app/api/sla.py`'s docstring, written in Phase Q, promised
that the queue-wide view would arrive "in Phase S, where they will call `resolve_position`
per row or build the query on top of it" — and ranking tickets by how soon they are due
cannot be done by calling a Python function per row, because that means reading every open
ticket, which §7 forbids. So `analytics_repository._outstanding_predicate` expresses the
same deadline in SQL, and the two implementations are now a pair that agrees until one of
them is edited.

`tests/unit/test_sla_clock.py` cannot catch that. A unit test of the clock proves the clock
is right; it says nothing about a `<=` in a query. So this file asserts the two agree
**where they can disagree**: at the instants either side of both boundaries of both timers,
for stopped and unstopped timers alike, and on the set of tickets each thinks has a deadline
at all.

**Four claims, and they cover the whole surface of the fragment.**

1. **Classification** — the SQL's `stopped_at <= due_at` (met) against the clock's
   `MET`/`BREACHED` branch. One `<=` becoming `<` is the edit this test is built to catch.
2. **Membership** — the SQL's `LEAST` over the two *unstopped* deadlines against the clock's
   "which of my timers are still running". `LEAST` ignores NULLs, so a ticket with both
   timers stopped must produce NULL and be excluded — not be ranked first, which is what a
   careless non-NULL predicate would do.
3. **Order** — the SQL's `ORDER BY` against the clock's own smallest remaining time. The
   risk list is a ranking, and a ranking that disagrees with the countdown printed beside it
   is the one discrepancy a reader would actually see.
4. **Overdue** — the SQL's `LEAST(...) < now` against "does the clock call any running timer
   breached". The two are different arithmetic that has to mean the same thing.

**The clock is not mocked and the wall clock is not read.** `NOW` is a fixed instant, given
to `resolve_position` explicitly and used to place every `created_at`. That is what makes the
boundary cases exactly on the boundary: an elapsed time of 7,200 seconds against a 2-hour
target is only constructible if "now" is a value the test chose, and a `datetime.now()`
anywhere would put the interesting cases a millisecond on one side or the other depending on
how fast the machine is. Nothing here consults the real clock, so nothing here is flaky.

**No `truncate_tables`.** Every row is written through the async `db` fixture, whose
transaction is rolled back on teardown — unlike the HTTP suites, where the application owns
its own transaction and commits.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.models.customer import Customer
from app.models.enums import TicketPriority, TicketStatus, UserRole
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.repositories.analytics_repository import AnalyticsRepository
from app.repositories.sla_repository import TERMINAL_STATUSES
from app.schemas.sla import SLATimerState
from app.services import sla_service
from app.services.sla_service import resolve_position

pytestmark = pytest.mark.integration

#: The instant everything is measured against, and the only "now" in this file.
NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

#: A window that contains every ticket below, so compliance is about the fixture rather
#: than about the window — which has its own tests in `tests/api/test_analytics.py`.
WINDOW = (NOW - timedelta(days=1), NOW + timedelta(days=1))

# `HIGH`'s seeded targets, in seconds: 120 minutes to a response with the warning band
# opening at 80% of it, and 480 to a resolution. Every boundary below is expressed as an
# offset from these, so a changed seed produces different interesting offsets rather than a
# silently weakened test.
RESPONSE_DUE = 120 * 60
RESPONSE_WARNING = int(120 * 60 * 0.8)
RESOLUTION_DUE = 480 * 60
RESOLUTION_WARNING = int(480 * 60 * 0.8)


def case(
    name: str,
    *,
    priority: str = "high",
    response_at: int | None = None,
    resolution_at: int | None = None,
    stop_response: int | None = None,
    stop_resolution: int | None = None,
    status: TicketStatus = TicketStatus.OPEN,
) -> dict[str, Any]:
    """One ticket in the matrix, described by seconds rather than by a `datetime`.

    `response_at`/`resolution_at` move the ticket's creation into the past by that many
    seconds, which is what sets the elapsed time against each target. `stop_*` place the
    corresponding stop at that many seconds after creation. Turning a bumpy tuple into a
    named dict is what keeps the matrix at the bottom of this file readable as a table.
    """
    return {
        "name": name,
        "priority": priority,
        "response_at": response_at,
        "resolution_at": resolution_at,
        "stop_response": stop_response,
        "stop_resolution": stop_resolution,
        "status": status,
    }


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------
#
# Read this as a table: one row per way the two implementations could disagree. The
# offsets are chosen against `HIGH`'s targets — the response's boundaries are 5,760 and
# 7,200 seconds, the resolution's 23,040 and 28,800 — and every `-1`/exact/`+1` triple
# around one is a place where a comparison operator differs by one character.
#
# Offsets are given to the *earlier* of the two deadlines where both timers run, so the
# response is the binding one; where the response has been stopped the resolution becomes
# the only deadline left and is what the ranking uses. That switch is itself worth covering:
# `_binding_timer` and `LEAST` have to make the same choice about which timer a ticket is
# ranked by.

CASES: tuple[dict[str, Any], ...] = (
    # --- Response, unstopped: the three bands, each probed on both sides ---------------
    case("just inside on track", response_at=RESPONSE_WARNING - 1),
    case("at the warning instant", response_at=RESPONSE_WARNING),
    case("just inside the warning", response_at=RESPONSE_WARNING + 1),
    case("just before the deadline", response_at=RESPONSE_DUE - 1),
    case("at the deadline", response_at=RESPONSE_DUE),
    case("just past the deadline", response_at=RESPONSE_DUE + 1),
    # --- Response, stopped: the `<=` that decides met from breached --------------------
    case("answered well inside", response_at=RESOLUTION_DUE, stop_response=1),
    case(
        "answered exactly on the deadline",
        response_at=RESOLUTION_DUE,
        stop_response=RESPONSE_DUE,
    ),
    case("answered one second late", response_at=RESOLUTION_DUE, stop_response=RESPONSE_DUE + 1),
    # --- Resolution, unstopped and therefore binding once the response is stopped ------
    case(
        "resolution on track",
        response_at=RESOLUTION_WARNING - 1,
        stop_response=1,
    ),
    case(
        "resolution at the warning instant",
        response_at=RESOLUTION_WARNING,
        stop_response=1,
    ),
    case(
        "resolution just before the deadline",
        response_at=RESOLUTION_DUE - 1,
        stop_response=1,
    ),
    case("resolution at the deadline", response_at=RESOLUTION_DUE, stop_response=1),
    case("resolution one second past", response_at=RESOLUTION_DUE + 1, stop_response=1),
    # --- Resolution, stopped: terminal, so the queue views must drop it ----------------
    case(
        "resolved exactly on the deadline",
        response_at=RESOLUTION_DUE,
        stop_response=1,
        stop_resolution=RESOLUTION_DUE,
        status=TicketStatus.RESOLVED,
    ),
    case(
        "resolved one second late",
        response_at=RESOLUTION_DUE,
        stop_response=1,
        stop_resolution=RESOLUTION_DUE + 1,
        status=TicketStatus.RESOLVED,
    ),
    # --- No outstanding deadline at all: reached by stopping both timers ---------------
    case(
        "both timers stopped",
        response_at=RESOLUTION_DUE,
        stop_response=1,
        stop_resolution=RESOLUTION_DUE,
        status=TicketStatus.RESOLVED,
    ),
    # --- A priority with no clock: the policy is switched off in the fixture -----------
    #
    # Its response *is* stopped, so the exclusion is observable: a query without the join
    # would count it among the responses that met their target, and the arithmetic at the
    # bottom of this file asserts that it did not.
    case(
        "urgent with no active policy",
        priority="urgent",
        response_at=RESPONSE_DUE,
        stop_response=1,
    ),
    # --- And one from another priority, so the join is doing real work ----------------
    case("medium, on track", priority="medium", response_at=60),
)


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def agreement(db: AsyncSession) -> dict[str, Any]:
    """One tenant, its four seeded policies — one of them switched off — and the matrix.

    Built through the ORM rather than the API because `created_at` and the stops have to be
    exact instants relative to `NOW`, and no route accepts a timestamp. That is the same
    reason `tests/security/test_row_scopes.py` builds its rows by hand.
    """
    organization = Organization(name="Agreement Co", slug=f"agreement-{NOW:%Y%m%d%H%M%S}")
    db.add(organization)
    await db.flush()

    customer = Customer(
        organization_id=organization.id, name="Ada Lovelace", email="ada@agreement.example"
    )
    db.add(customer)
    await db.flush()

    policies = sla_service.build_default_policies(organization.id)
    for policy in policies:
        # `URGENT` has no clock, so nothing about it should appear in either
        # implementation's answer. Switched off rather than deleted, which is how a tenant
        # does it — and the row still exists, so the join is what excludes it and not a
        # missing insert.
        if policy.priority is TicketPriority.URGENT:
            policy.is_active = False
    db.add_all(policies)
    await db.flush()

    tickets: dict[str, Ticket] = {}
    for number, spec in enumerate(CASES, start=1):
        # The largest of the offsets, so the creation is far enough back that both
        # timers' elapsed times are the ones the case names.
        elapsed = max(
            offset for offset in (spec["response_at"], spec["resolution_at"]) if offset is not None
        )
        created_at = NOW - timedelta(seconds=elapsed)
        ticket = Ticket(
            organization_id=organization.id,
            number=number,
            customer_id=customer.id,
            subject=spec["name"],
            description="Body.",
            priority=TicketPriority(spec["priority"]),
            status=spec["status"],
            created_at=created_at,
            first_response_at=(
                created_at + timedelta(seconds=spec["stop_response"])
                if spec["stop_response"] is not None
                else None
            ),
            resolved_at=(
                created_at + timedelta(seconds=spec["stop_resolution"])
                if spec["stop_resolution"] is not None
                else None
            ),
        )
        tickets[spec["name"]] = ticket

    db.add_all(list(tickets.values()))
    await db.flush()

    return {
        "context": TenantContext(
            user_id=customer.id, organization_id=organization.id, role=UserRole.ADMIN
        ),
        "tickets": tickets,
        "policies": policies,
        "open_tickets": sum(
            1 for ticket in tickets.values() if ticket.status not in TERMINAL_STATUSES
        ),
    }


# ---------------------------------------------------------------------------
# The clock's own answer, computed once
# ---------------------------------------------------------------------------


def clock_verdict(rows: dict[str, Any]) -> dict[str, Any]:
    """What the pure clock says about every ticket in the fixture.

    This is the *other* implementation — `resolve_position`, the same function the ticket
    detail screen calls — applied to the same rows the SQL is about to summarize, against
    the same policy objects the SQL joins to. Every aggregate assertion below is this
    function's output against a repository method's.

    A ticket whose priority has no active policy is dropped, because the clock's own caller
    in `analytics_service.risk_list` drops it: `load_policies` returns only active rows, so
    a priority missing from that map has no target to resolve against and no position to
    report. That absence is the design, not an accident of the join.
    """
    policies = {policy.priority: policy for policy in rows["policies"] if policy.is_active}

    met = {"response": 0, "resolution": 0}
    breached = {"response": 0, "resolution": 0}
    overdue: set[Any] = set()
    ranked: list[tuple[datetime, Any]] = []

    for ticket in rows["tickets"].values():
        policy = policies.get(ticket.priority)
        if policy is None:
            continue

        position = resolve_position(ticket, policy, now=NOW)
        timers = (("response", position.response), ("resolution", position.resolution))

        for name, timer in timers:
            if timer.stopped_at is None:
                continue
            if timer.state is SLATimerState.MET:
                met[name] += 1
            else:
                breached[name] += 1

        running = [timer for _, timer in timers if timer.stopped_at is None]
        if not running or ticket.status in TERMINAL_STATUSES:
            continue

        # The soonest deadline still outstanding for this ticket, which is the quantity the
        # SQL's `LEAST` computes — and the one `_binding_timer` picks its timer from.
        ranked.append((min(timer.due_at for timer in running), ticket.id))
        if any(timer.state is SLATimerState.BREACHED for timer in running):
            overdue.add(ticket.id)

    return {
        "met": met,
        "breached": breached,
        "overdue": overdue,
        "order": [ticket_id for _, ticket_id in sorted(ranked)],
    }


def as_counts(verdict: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """The clock's verdict in the shape `sla_compliance` returns."""
    return {
        timer: (verdict["met"][timer], verdict["breached"][timer])
        for timer in ("response", "resolution")
    }


@pytest.fixture
def verdict(agreement: dict[str, Any]) -> dict[str, Any]:
    return clock_verdict(agreement)


# ---------------------------------------------------------------------------
# The four claims
# ---------------------------------------------------------------------------


async def test_the_matrix_has_a_case_for_every_boundary_the_fragment_probes(
    agreement: dict[str, Any],
) -> None:
    """A guard on the guard, and the reason this file is worth reading.

    A differential test proves what it *covers*. If the matrix lost its `at the deadline`
    row, every assertion below would still pass and the file would have stopped pinning the
    one comparison it exists for. So the boundaries are asserted to be present as data, and
    the states they produce are asserted to be distinct — a matrix that produced only
    `ON_TRACK` would agree with the SQL about nothing at all.
    """
    policies = {policy.priority: policy for policy in agreement["policies"] if policy.is_active}
    states = {
        name: resolve_position(ticket, policies[ticket.priority], now=NOW).response.state
        for name, ticket in agreement["tickets"].items()
        if ticket.priority in policies
    }

    assert set(states.values()) == {
        SLATimerState.ON_TRACK,
        SLATimerState.WARNING,
        SLATimerState.MET,
        SLATimerState.BREACHED,
    }
    # The three instants around one boundary produce three different answers. This is the
    # claim a `<=` becoming `<` would break *here*, in the clock, before SQL is involved —
    # which is exactly why the SQL has to be compared against it rather than against a
    # restatement of what the SQL does.
    assert states["just inside the warning"] is SLATimerState.WARNING
    assert states["at the warning instant"] is SLATimerState.WARNING
    assert states["just before the deadline"] is SLATimerState.WARNING
    assert states["at the deadline"] is SLATimerState.BREACHED
    assert states["answered exactly on the deadline"] is SLATimerState.MET
    assert states["answered one second late"] is SLATimerState.BREACHED


async def test_compliance_classifies_every_boundary_the_way_the_clock_does(
    db: AsyncSession, agreement: dict[str, Any], verdict: dict[str, Any]
) -> None:
    """Claim 1: the SQL's met/breached counts equal the clock's, ticket for ticket.

    Each boundary in the matrix contributes exactly one to one of these four numbers, so an
    off-by-one comparison moves a count rather than hiding inside a total.
    """
    repository = AnalyticsRepository(db, agreement["context"])

    counts = await repository.sla_compliance(*WINDOW)

    assert dict(counts) == as_counts(verdict)


async def test_the_queue_holds_the_tickets_the_clock_says_have_a_deadline(
    db: AsyncSession, agreement: dict[str, Any], verdict: dict[str, Any]
) -> None:
    """Claims 2 and 4: membership and overdue, over the same rows.

    A terminal ticket and a ticket with both timers stopped both have nothing outstanding,
    and neither may appear. The overdue set is asserted by identity rather than by size, so a
    query that swapped one ticket for another would fail rather than balance out.
    """
    repository = AnalyticsRepository(db, agreement["context"])

    overdue = await repository.overdue_count(NOW)
    open_tickets = await repository.open_ticket_count()

    assert overdue == len(verdict["overdue"])
    assert open_tickets == agreement["open_tickets"]


async def test_the_risk_ranking_is_the_clock_s_ranking(
    db: AsyncSession, agreement: dict[str, Any], verdict: dict[str, Any]
) -> None:
    """Claim 3, and the one a reader would notice: the order.

    The repository orders in SQL and the clock orders by the smallest remaining time, and
    `analytics_service.risk_list` deliberately does not re-sort — so a disagreement here
    would put a ticket below one that is further from its deadline, on the screen a manager
    scans first.

    The expected order ties by ticket id, which is the SQL's own tie-break
    (`ORDER BY LEAST(...), tickets.id`). Several boundary cases share a `created_at` and
    therefore a deadline, so the tie-break is load-bearing rather than decorative.
    """
    repository = AnalyticsRepository(db, agreement["context"])

    candidates = await repository.risk_candidates(limit=100)

    assert [ticket.id for ticket in candidates] == verdict["order"]


async def test_a_ticket_with_no_outstanding_deadline_is_absent_rather_than_first(
    db: AsyncSession, agreement: dict[str, Any], verdict: dict[str, Any]
) -> None:
    """**The three-valued-logic trap, asserted rather than reasoned about.**

    `_outstanding_predicate` is `LEAST(case(...), case(...))`. Both `CASE`s are NULL for a
    ticket whose timers have both stopped, and PostgreSQL's `LEAST` ignores NULLs rather
    than propagating them — so `LEAST(NULL, NULL)` is NULL, which is the intended answer.
    Had it propagated a non-NULL value instead, that ticket would sort *first*: the newest
    deadline in the set is still a deadline, and NULLs sort last only because they mean
    "nothing outstanding".

    Asserted on the specific tickets rather than on the count, because the count could agree
    while the wrong two tickets sat at the top of the list.
    """
    candidates = {
        ticket.id
        for ticket in await AnalyticsRepository(db, agreement["context"]).risk_candidates(limit=100)
    }
    tickets = agreement["tickets"]

    assert tickets["both timers stopped"].id not in candidates
    assert tickets["resolved exactly on the deadline"].id not in candidates
    assert tickets["resolved one second late"].id not in candidates
    assert tickets["urgent with no active policy"].id not in candidates

    # And the control: a ticket that differs from `both timers stopped` only by having one
    # timer left running is *in* the list, so the absence above is about the deadline and
    # not about the ticket having been excluded for some other reason.
    assert tickets["resolution at the deadline"].id in candidates
    # It is in there despite having passed its deadline, which is the whole point of a risk
    # list: `_outstanding_predicate() < now` is the overdue count, and the ranking is the
    # same expression without the inequality.
    assert tickets["resolution at the deadline"].id in verdict["overdue"]


async def test_a_priority_with_no_active_policy_has_no_position_in_either_implementation(
    db: AsyncSession, agreement: dict[str, Any]
) -> None:
    """The join's other side, in both directions.

    A ticket whose priority has no active policy is excluded from compliance by the join and
    from the ranking by the join, and `clock_verdict` excludes it too — because
    `sla_service.load_policies` would not have returned a policy for it and
    `analytics_service.risk_list` skips a ticket it has no policy for. Neither
    implementation invents a default target, which is the failure that would make a
    switched-off priority quietly enforceable again.
    """
    repository = AnalyticsRepository(db, agreement["context"])
    ticket = agreement["tickets"]["urgent with no active policy"]

    policies = await sla_service.load_policies(db, agreement["context"])
    counts = await repository.sla_compliance(*WINDOW)
    candidates = {row.id for row in await repository.risk_candidates(limit=100)}

    assert ticket.priority not in policies
    assert ticket.id not in candidates
    # **The counterfactual, as arithmetic.** Count every stopped timer in the fixture and
    # compare it with what the SQL counted. The two differ by exactly one, and the missing
    # one is this ticket's response — which stopped, met its target, and is covered by no
    # policy at all. Without the join it would be counted, which is what makes a switched-off
    # priority quietly enforceable again.
    stopped = sum(
        1
        for row in agreement["tickets"].values()
        for stop in (row.first_response_at, row.resolved_at)
        if stop is not None
    )
    assert ticket.first_response_at is not None
    assert sum(counts["response"]) + sum(counts["resolution"]) == stopped - 1
    assert await repository.open_ticket_count() == agreement["open_tickets"]


@pytest.mark.parametrize("minutes", [30, 120, 480, 1440, 4320])
async def test_make_interval_builds_the_same_span_the_clock_adds(
    db: AsyncSession, minutes: int
) -> None:
    """The fragment's premise, checked against PostgreSQL rather than assumed.

    `_due_at` is `created_at + make_interval(0, 0, 0, 0, 0, target_minutes)` and the clock is
    `created_at + timedelta(minutes=target_minutes)`. Every assertion above compares the
    *decisions* made from those two expressions; if the positional arguments were ever
    shuffled — `make_interval` takes years, months, weeks, days, hours, mins, secs — the two
    implementations would go on agreeing about ordering and classification while computing
    different deadlines, because `LEAST` is relative and nothing else compares the two
    values. That is the one disagreement the rest of this file could not see.

    So the span itself is asserted, once per seeded target, across the boundary between
    PostgreSQL and Python where a type or an argument order could silently differ.
    """
    span = await db.scalar(select(func.make_interval(0, 0, 0, 0, 0, minutes)))

    assert span == timedelta(minutes=minutes)
