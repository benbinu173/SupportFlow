"""The five analytics routes over HTTP: the numbers, the window, and the refusals.

This is the read half of Phase S. The aggregates' SQL is exercised here against a small
fixture whose arithmetic is worked out in the test, which is what makes a wrong `GROUP BY`
observable rather than merely plausible: every expected number below is derived from the
tickets the test created, never from a stored constant.

**Every test's tickets are created through the API and aged with SQL.** Backdating
`created_at` is the only way to move the clock — there is no cached position to invalidate
and no second copy to keep in step, and a `created_at` far enough in the past is a ticket
that has been open that long as far as every reader is concerned. That is the same trick
`tests/api/test_ticket_sla.py` uses, for the same reason, and the fixture numbers below are
chosen to land one timer in a state and leave the other where it is.

**Two tests here are about the cache rather than about arithmetic** — that a new ticket and
a public reply each move the next reading — because the invalidation call sites are the one
part of this phase that fails *silently*: a missing `cache.invalidate` produces perfectly
correct numbers that are up to a TTL out of date, and nothing else in the suite would
notice. `tests/unit/test_analytics_cache.py` proves the mechanism; these prove it is wired.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.conftest import API, SLA, TICKETS, OrgSession

pytestmark = pytest.mark.integration

ANALYTICS = f"{API}/analytics"

# §27's seeded targets, which every expectation below is derived from. Restated rather
# than imported from `sla_service.DEFAULT_POLICIES` so that changing a seed is a *failing
# test* rather than a silent change of what these tests were asserting.
LOW = (24 * 60, 72 * 60)
MEDIUM = (8 * 60, 24 * 60)
HIGH = (2 * 60, 8 * 60)

# The paths, so the parametrized refusal test cannot fall behind a sixth route by being
# written as a list of URLs somebody forgets to extend. `test_route_protection.py` walks
# the routing table for the same reason; this one is about what a *customer* gets.
PATHS = ("/overview", "/tickets", "/agents", "/sla", "/sentiment")


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    """The registered admin. Also the session that may edit SLA policies, which is
    `SLA_CONFIGURE` and therefore admin-only."""
    return register_org(organization_name="Metrics Co")


@pytest.fixture
def manager(org: OrgSession) -> OrgSession:
    """Reaches the whole organization's rows without holding `SLA_CONFIGURE`."""
    return org.add_user("manager", email="manager@metricsco.com")


@pytest.fixture
def agent(org: OrgSession) -> OrgSession:
    """The assignee. `AGENT` is `RowScope.ASSIGNED`, so their analytics reach exactly the
    tickets assigned to them — which is what the scope tests below turn on."""
    return org.add_user("agent", name="Ana", email="ana@metricsco.com")


@pytest.fixture
def portal(org: OrgSession) -> dict[str, Any]:
    record = org.add_customer(name="Grace Hopper", email="grace@navy.mil")
    return {"record": record, "session": org.add_portal_user(cast("str", record["id"]))}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def read(session: OrgSession, path: str, **params: Any) -> dict[str, Any]:
    """A successful analytics read. Any non-200 is a failure of the test, not of a check."""
    response = session.get(f"{ANALYTICS}{path}", params=params or None)
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def status_of(session: OrgSession, path: str, **params: Any) -> int:
    """The status code alone, for the requests that are supposed to be refused."""
    return int(session.get(f"{ANALYTICS}{path}", params=params or None).status_code)


def raise_ticket(
    session: OrgSession, customer_id: object, *, priority: str = "medium", **kwargs: Any
) -> dict[str, Any]:
    return session.add_ticket(cast("str", customer_id), priority=priority, **kwargs)


def backdate(engine: Engine, ticket_id: object, *, minutes: int) -> None:
    """Move a ticket's creation into the past, which is the only way to age the clock.

    `make_interval(mins => …)` rather than a literal so the count stays an integer
    parameter and the statement stays parameterized. Only `created_at` moves: the two
    stops (`first_response_at`, `resolved_at`) are what they are, so the interval between
    them and the creation is exactly the number of minutes passed here.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tickets SET created_at = created_at - make_interval(mins => :minutes) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(ticket_id), "minutes": minutes},
        )


def assign(session: OrgSession, ticket_id: object, assignee: OrgSession) -> None:
    response = session.post(
        f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": assignee.user_id}
    )
    assert response.status_code == 200, response.text


def resolve(session: OrgSession, ticket_id: object, assignee: OrgSession) -> None:
    """Walk the lifecycle's own edges to `RESOLVED`, which is where `resolved_at` is set.

    Three requests rather than a status write, because there is no route that jumps the
    lifecycle: `IN_PROGRESS` is only reachable from `ASSIGNED`, and `POST /tickets/{id}/
    status` refuses the edges assignment owns.
    """
    assign(session, ticket_id, assignee)
    for status in ("in_progress", "resolved"):
        response = session.post(f"{TICKETS}/{ticket_id}/status", json={"status": status})
        assert response.status_code == 200, response.text


def reply(session: OrgSession, ticket_id: object) -> None:
    """A public staff reply, which is what stops the response clock."""
    response = session.post(f"{TICKETS}/{ticket_id}/messages", json={"body": "Looking at it."})
    assert response.status_code == 201, response.text


def row_for(body: dict[str, Any], who: OrgSession | None) -> dict[str, Any]:
    """One agent's row, found by id rather than by position.

    The unassigned bucket is a row like any other and can sort before or after a named
    agent, so indexing `agents[0]` would make these tests depend on a NULLS ordering they
    are not about. `who=None` finds the bucket with no assignee.
    """
    wanted = who.user_id if who is not None else None
    agents = cast("list[dict[str, Any]]", body["agents"])
    rows = [row for row in agents if row["agent_id"] == wanted]
    assert len(rows) == 1, f"expected exactly one row for {wanted}, got {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def test_the_range_is_resolved_and_echoed_back(manager: OrgSession) -> None:
    """A request that named nothing is told what it got.

    `range` is what makes every number on a response self-describing: a client that
    screenshots a total without it has no way to say which month it was.
    """
    body = read(manager, "/overview")

    assert body["range"]["bucket"] == "day"
    # Aware, and ordered — the two properties the SQL comparison depends on. A naive
    # `start` returned here would be the response disagreeing with its own query.
    parsed = datetime.fromisoformat(body["range"]["start"])
    assert parsed.tzinfo is not None
    assert parsed < datetime.fromisoformat(body["range"]["end"])


def test_a_start_after_its_end_is_refused(manager: OrgSession) -> None:
    """422 with a sentence rather than an empty window.

    An inverted range would otherwise answer with zeroes, which reads as "this tenant did
    nothing" — the most misleading possible answer to a typo.
    """
    code = status_of(
        manager,
        "/overview",
        start="2026-03-01T00:00:00Z",
        end="2026-02-01T00:00:00Z",
    )

    assert code == 422


def test_a_naive_bound_is_refused(manager: OrgSession) -> None:
    """**Refused rather than interpreted**, because psycopg does not raise on the
    comparison a naive bound produces — it applies the session's timezone and returns a
    number shifted by hours, with nothing on the response to explain it."""
    assert status_of(manager, "/overview", start="2026-02-01T00:00:00") == 422


def test_a_window_longer_than_a_year_is_refused(manager: OrgSession) -> None:
    """A ceiling, not a truncation. Silently shortening it would make a chart lie about
    its own x-axis, which is worse than an error a client can act on."""
    code = status_of(
        manager,
        "/overview",
        start="2024-01-01T00:00:00Z",
        end="2026-01-01T00:00:00Z",
    )

    assert code == 422


def test_an_explicit_window_returns_exactly_what_was_asked_for(manager: OrgSession) -> None:
    """The echoed range is the caller's, not a default with the parameters ignored."""
    body = read(
        manager,
        "/overview",
        start="2026-02-01T00:00:00Z",
        end="2026-02-08T00:00:00Z",
    )

    assert body["range"]["start"].startswith("2026-02-01T00:00:00")
    assert body["range"]["end"].startswith("2026-02-08T00:00:00")
    # And the numbers describe that window, so a ticket created today is outside it.
    assert body["totals"]["total"] == 0


def test_a_range_is_half_open_so_adjacent_windows_tile(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """`start` inclusive, `end` exclusive — a ticket on a boundary belongs to one window.

    Asserted through the API because this is the convention a client paginating by day
    depends on, and an inclusive `end` would double-count the boundary ticket in both.
    The boundary is the ticket's own `created_at`, read back from the API, so the two
    windows differ in nothing but which side of it is closed.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    created = datetime.fromisoformat(ticket["created_at"])
    second = timedelta(seconds=1)

    opened_here = read(
        manager,
        "/overview",
        start=created.isoformat(),
        end=(created + second).isoformat(),
    )
    closed_here = read(
        manager,
        "/overview",
        start=(created - second).isoformat(),
        end=created.isoformat(),
    )

    assert opened_here["totals"]["total"] == 1
    assert closed_here["totals"]["total"] == 0


# ---------------------------------------------------------------------------
# Overview: totals, volume, averages, AI usage
# ---------------------------------------------------------------------------


def test_the_totals_count_what_the_house_contains(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any]
) -> None:
    """Three tickets, one of them resolved: two open, one terminal.

    `open` is derived as `total - terminal` rather than counted separately, so the
    breakdown and the summary cannot disagree — which is asserted here by recomputing the
    summary from the breakdown in the test.
    """
    raise_ticket(manager, portal["record"]["id"], priority="high")
    raise_ticket(manager, portal["record"]["id"], priority="low")
    done = raise_ticket(manager, portal["record"]["id"])
    resolve(manager, done["id"], agent)

    totals = read(manager, "/overview")["totals"]

    assert totals["total"] == 3
    assert totals["resolved"] == 1
    assert totals["closed"] == 0
    assert totals["open"] == 2
    assert sum(totals["by_status"].values()) == totals["total"]


def test_every_status_is_present_even_where_nothing_is_counted(manager: OrgSession) -> None:
    """A `GROUP BY` returns only the values that occur; the breakdown is completed here.

    Without it a chart's series would grow the first time somebody moves a ticket to
    `WAITING_FOR_CUSTOMER`, and a client with a fixed legend would silently drop the bar.
    """
    by_status = read(manager, "/overview")["totals"]["by_status"]

    assert set(by_status) == {
        "open",
        "assigned",
        "in_progress",
        "waiting_for_customer",
        "resolved",
        "closed",
    }
    assert all(count == 0 for count in by_status.values())


def test_the_volume_series_adds_up_to_the_total(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """One point per day that has tickets, ascending, and none of them negative.

    The sum is asserted rather than the bucket count because the buckets are UTC days and
    two tickets raised either side of midnight are legitimately two points — this test
    must not be the one that fails at 23:59.
    """
    for _ in range(4):
        raise_ticket(manager, portal["record"]["id"])

    body = read(manager, "/overview")
    buckets = [point["bucket"] for point in body["volume"]]

    assert sum(point["count"] for point in body["volume"]) == body["totals"]["total"] == 4
    assert buckets == sorted(buckets)
    assert len(set(buckets)) == len(buckets)


def test_an_average_with_nothing_behind_it_is_null_not_zero(manager: OrgSession) -> None:
    """**The distinction that keeps a dashboard honest.** No rows means the average does
    not exist; rendering it as zero would report an instantly-answering desk."""
    body = read(manager, "/overview")

    assert body["response_time"] == {"average_seconds": None, "count": 0}
    assert body["resolution_time"] == {"average_seconds": None, "count": 0}


def test_the_response_average_is_the_interval_the_timestamps_imply(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """Replied ten hours after creation, so the average is 36,000 seconds and a round trip.

    The ticket is aged *before* the reply, which is what makes the interval the backdate
    rather than "however long the two requests took": the reply sets `first_response_at`
    to the wall clock and the creation is the only timestamp this test moves. The reply is
    still one HTTP round trip later than the creation was, so the interval is the backdate
    **plus a few milliseconds** — never less than it, which is the direction that matters,
    and the reason this is a bounded assertion rather than an equality.
    """
    ticket = raise_ticket(manager, portal["record"]["id"], priority="high")
    backdate(sync_engine, ticket["id"], minutes=600)
    reply(manager, ticket["id"])

    summary = read(manager, "/overview")["response_time"]

    assert summary["count"] == 1
    average = cast("int", summary["average_seconds"])
    assert 600 * 60 <= average <= 600 * 60 + 10


def test_the_resolution_average_is_the_interval_the_timestamps_imply(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """Resolved five hours after creation — measured from `created_at`, which is where
    both SLA timers start, so this average and the compliance percentages describe one
    clock rather than two."""
    ticket = raise_ticket(manager, portal["record"]["id"])
    backdate(sync_engine, ticket["id"], minutes=300)
    resolve(manager, ticket["id"], agent)

    summary = read(manager, "/overview")["resolution_time"]

    assert summary["count"] == 1
    average = cast("int", summary["average_seconds"])
    assert 300 * 60 <= average <= 300 * 60 + 10


def test_the_ai_usage_block_is_a_real_zero(manager: OrgSession) -> None:
    """§8's eleventh criterion: real aggregation queries, never hardcoded values.

    Nothing writes `ai_usage` until Phase T, so these are `COUNT` and `SUM` results over
    zero rows — which is why `failed_calls` and `cached_calls` are numbers of their own and
    why `by_operation` is empty rather than carrying enum rows at zero: an operation with no
    calls has no row to group, and inventing one would be the one hardcoded value in this
    module.

    `cost_usd` is compared as a `Decimal` rather than as its rendered string. A sum over
    no rows is `NULL`, coalesced to zero — and PostgreSQL renders that zero as `"0"`, not
    as the six-place `"0.000000"` a real sum carries, because the scale comes from the
    value. Asserting the parsed number is what keeps this test about the money being
    nothing rather than about its formatting.

    A tenant that has made no calls has also saved none, which is what `cached_calls` being
    zero here says — §20's summary cache is a fact about a row that was *not* written, and a
    tenant with no rows at all has neither claim to make.
    """
    usage = read(manager, "/overview")["ai_usage"]

    assert Decimal(usage.pop("cost_usd")) == Decimal(0)
    assert usage == {
        "calls": 0,
        "failed_calls": 0,
        "cached_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "by_operation": [],
    }


# ---------------------------------------------------------------------------
# Tickets by priority and category
# ---------------------------------------------------------------------------


def test_the_priority_breakdown_is_complete_and_summed(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    raise_ticket(manager, portal["record"]["id"], priority="high")
    raise_ticket(manager, portal["record"]["id"], priority="high")
    raise_ticket(manager, portal["record"]["id"], priority="low")

    by_priority = read(manager, "/tickets")["by_priority"]

    assert [(row["priority"], row["count"]) for row in by_priority] == [
        ("low", 1),
        ("medium", 0),
        ("high", 2),
        ("urgent", 0),
    ]


def test_the_category_breakdown_groups_the_category_that_was_set(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """Categories are free text written by AI classification in Phase U, so this is a
    top-N rather than an enum-complete list — and until then the `None` bucket is where
    every ticket is, which is a fact about the tenant and not a placeholder."""
    raise_ticket(manager, portal["record"]["id"], category="billing")
    raise_ticket(manager, portal["record"]["id"])

    by_category = read(manager, "/tickets")["by_category"]

    # A set rather than a sorted list: `None` and a string are not orderable against each
    # other, and comparing sets is what keeps this test about the grouping rather than
    # about the route's `ORDER BY count DESC, category ASC NULLS LAST` tie-break.
    assert {(row["category"], row["count"]) for row in by_category} == {(None, 1), ("billing", 1)}


def test_the_sentiment_distribution_names_the_unanalysed_bucket(manager: OrgSession) -> None:
    """One `None` bucket and three zeroes until Phase U.

    `None` is present rather than omitted so `unanalysed` is a visible number: a
    distribution that covered only classified tickets would show a percentage of an
    undefined whole.
    """
    body = read(manager, "/sentiment")

    assert body["unanalysed"] == 0
    assert body["analysed"] == 0
    assert body["buckets"] == [
        {"sentiment": None, "count": 0},
        {"sentiment": "positive", "count": 0},
        {"sentiment": "neutral", "count": 0},
        {"sentiment": "negative", "count": 0},
    ]


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_the_agent_breakdown_lists_the_unassigned_bucket(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any]
) -> None:
    """`None` is a row rather than an omission: "how much work has nobody picked up" is a
    manager's first question, and hiding it would leave the rows not adding up to the
    total with nothing to explain the difference."""
    picked_up = raise_ticket(manager, portal["record"]["id"])
    raise_ticket(manager, portal["record"]["id"])
    assign(manager, picked_up["id"], agent)

    body = read(manager, "/agents")

    assert body["total"] == 2
    assert len(body["agents"]) == 2
    unassigned = row_for(body, None)
    assert unassigned["assigned"] == 1
    assert unassigned["agent_name"] is None
    assert row_for(body, agent)["assigned"] == 1


def test_the_agent_breakdown_names_the_agent_and_counts_their_work(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any]
) -> None:
    done = raise_ticket(manager, portal["record"]["id"])
    open_one = raise_ticket(manager, portal["record"]["id"])
    resolve(manager, done["id"], agent)
    assign(manager, open_one["id"], agent)

    row = row_for(read(manager, "/agents"), agent)

    assert row["agent_name"] == "Ana"
    assert row["assigned"] == 2
    assert row["resolved"] == 1
    assert row["open"] == 1


def test_the_agent_breakdown_is_refused_to_an_agent(agent: OrgSession) -> None:
    """`ANALYTICS_ORG` and not `ANALYTICS_OWN`. §3's line is that reading the tenant's own
    numbers is an agent's capability and comparing agents against each other is not.

    403 rather than 404: the caller is in the tenant and the route exists, so this is a
    capability they lack rather than a resource that is not there — the same distinction
    `require_permission` draws everywhere else.
    """
    assert status_of(agent, "/agents") == 403


def test_the_agent_breakdown_pages_with_a_total_for_the_unpaged_list(
    org: OrgSession, manager: OrgSession, portal: dict[str, Any]
) -> None:
    """ "Showing 2 of 3" without a second request — which is why `total` is the row count
    before paging rather than `len(agents)`.

    The three agents are created by the admin, because `USER_CREATE` is not a manager's
    capability; the assignment is the manager's, which is the part under test.
    """
    for index in range(3):
        contributor = org.add_user("agent", name=f"Agent {index}")
        ticket = raise_ticket(manager, portal["record"]["id"])
        assign(manager, ticket["id"], contributor)

    body = read(manager, "/agents", limit=2, offset=0)
    second_page = read(manager, "/agents", limit=2, offset=2)

    assert body["total"] == second_page["total"] == 3
    assert len(body["agents"]) == 2
    assert len(second_page["agents"]) == 1
    assert {row["agent_id"] for row in body["agents"]}.isdisjoint(
        {row["agent_id"] for row in second_page["agents"]}
    )


def test_a_limit_over_the_cap_is_refused(manager: OrgSession) -> None:
    """`le=` on the query parameter, so the ceiling is declared where it is read."""
    assert status_of(manager, "/agents", limit=101) == 422


# ---------------------------------------------------------------------------
# SLA: compliance, the standing queue, and the risk ranking
# ---------------------------------------------------------------------------


def test_the_two_routes_are_absent_for_a_tenant_with_no_tickets(manager: OrgSession) -> None:
    """No stopped timer, so no rate — `None` rather than `1.0`.

    A fabricated perfect record is the failure this guards: a tenant that has answered
    nothing must not read as a tenant that has answered everything on time.
    """
    body = read(manager, "/sla")

    assert body["compliance"]["rate"] is None
    assert body["compliance"]["response"] == {
        "timer": "response",
        "met": 0,
        "breached": 0,
        "rate": None,
    }
    assert body["overdue"] == 0
    assert body["open_tickets"] == 0
    assert body["risks"] == []


def test_compliance_counts_each_timer_separately_and_pools_them(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """Four tickets, four different histories, and every number worked out here.

    * `answered` — replied at once, so the response was inside its target. Met.
    * `late` — aged well past both of `HIGH`'s targets and *then* replied, so the response
      was thousands of minutes against a `HIGH[0]`-minute target. Breached. Its resolution
      timer never stops, so it is not in the resolution count at all.
    * `done` — aged two thirds of the way to `LOW`'s response target and resolved against
      `LOW`'s resolution target. The resolution count's one met; the response timer never
      stops, so it is not in the response count.
    * `waiting` — untouched. Neither timer is in either count.

    So the pooled rate is 2 met of 3 stopped, and the two timers' rates differ — which is
    the reason the breakdown exists rather than one number.
    """
    answered = raise_ticket(manager, portal["record"]["id"], priority="high")
    reply(manager, answered["id"])

    late = raise_ticket(manager, portal["record"]["id"], priority="high")
    backdate(sync_engine, late["id"], minutes=HIGH[1] * 4)
    reply(manager, late["id"])

    done = raise_ticket(manager, portal["record"]["id"], priority="low")
    # Three quarters of the way to `LOW[0]`, so the response timer is still running, and
    # well inside `LOW[1]`, so the resolution is met.
    backdate(sync_engine, done["id"], minutes=LOW[0] * 3 // 4)
    resolve(manager, done["id"], agent)

    raise_ticket(manager, portal["record"]["id"], priority="medium")

    compliance = read(manager, "/sla")["compliance"]

    assert compliance["response"] == {
        "timer": "response",
        "met": 1,
        "breached": 1,
        "rate": 0.5,
    }
    assert compliance["resolution"] == {
        "timer": "resolution",
        "met": 1,
        "breached": 0,
        "rate": 1.0,
    }
    assert compliance["met"] == 2
    assert compliance["breached"] == 1
    assert compliance["rate"] == pytest.approx(2 / 3)


def test_compliance_counts_the_cohort_the_volume_chart_describes(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """The window is the ticket's *creation*, not the date it was answered.

    A ticket created before the window and resolved inside it is outside both numbers —
    so the rate and the volume chart describe one set of tickets, and a reader comparing
    them is comparing something.
    """
    stale = raise_ticket(manager, portal["record"]["id"], priority="low")
    backdate(sync_engine, stale["id"], minutes=60 * 24 * 40)
    resolve(manager, stale["id"], agent)

    assert read(manager, "/overview")["totals"]["total"] == 0
    assert read(manager, "/sla")["compliance"]["resolution"]["met"] == 0


def test_the_overdue_count_reads_the_queue_rather_than_the_window(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """**The two counts are deliberately not windowed.** A ticket raised forty days ago
    and still past its deadline is the one a manager most needs to see; hiding it because
    it fell outside a date range would be the worst possible omission.

    So `totals` says zero and `open_tickets` says one, on the same tenant, in the same
    response pair — which is the distinction `AnalyticsSLA`'s docstring describes.
    """
    stale = raise_ticket(manager, portal["record"]["id"], priority="high")
    backdate(sync_engine, stale["id"], minutes=60 * 24 * 40)

    body = read(manager, "/sla")

    assert body["open_tickets"] == 1
    assert body["overdue"] == 1
    assert read(manager, "/overview")["totals"]["total"] == 0


def test_a_ticket_inside_its_deadline_is_not_overdue(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """The control for the test above, and the one that shows the warning band works.

    Aged past `MEDIUM[0]`'s 80% threshold (the model's `warning_threshold_percent`
    default) but short of the target itself — so the count stays zero, the ticket is
    still ranked, and its state is `warning` rather than `on_track`. That band is the
    reason an agent is told while there is still time to act, and it is the one SLA state
    this file asserts over HTTP; `tests/unit/test_sla_clock.py` proves the boundaries.
    """
    fresh = raise_ticket(manager, portal["record"]["id"], priority="medium")
    backdate(sync_engine, fresh["id"], minutes=MEDIUM[0] * 5 // 6)

    body = read(manager, "/sla")

    assert body["overdue"] == 0
    assert body["open_tickets"] == 1
    assert len(body["risks"]) == 1
    assert body["risks"][0]["state"] == "warning"
    assert body["risks"][0]["remaining_seconds"] > 0


def test_the_risk_list_is_ranked_worst_first(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """The soonest outstanding deadline first — which is `LEAST` over the two unstopped
    timers, computed in SQL. The ticket that is already past due leads, and the
    remaining times ascend, so the ranking is checkable rather than merely plausible.
    """
    middle = raise_ticket(manager, portal["record"]["id"], priority="high")
    worst = raise_ticket(manager, portal["record"]["id"], priority="high")
    best = raise_ticket(manager, portal["record"]["id"], priority="low")
    backdate(sync_engine, worst["id"], minutes=60 * 24 * 5)
    backdate(sync_engine, middle["id"], minutes=60 * 8)

    risks = read(manager, "/sla")["risks"]

    assert [risk["id"] for risk in risks] == [worst["id"], middle["id"], best["id"]]
    remaining = [risk["remaining_seconds"] for risk in risks]
    assert remaining == sorted(remaining)
    assert remaining[0] < 0  # already past its deadline


def test_the_binding_timer_is_the_sooner_of_the_two(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """A ticket ten hours old against a 2-hour response and an 8-hour resolution target.

    Both deadlines have passed, and the response's passed first — so the response is what
    the row reports. `LEAST` picks it, and `_binding_timer` independently agrees, which is
    the pair of decisions this phase keeps deliberately separate.
    """
    stale = raise_ticket(manager, portal["record"]["id"], priority="high")
    backdate(sync_engine, stale["id"], minutes=HIGH[0] * 5)

    risk = read(manager, "/sla")["risks"][0]

    assert risk["timer"] == "response"
    assert risk["state"] == "breached"
    assert risk["remaining_seconds"] < 0


def test_a_risk_row_carries_the_same_clock_as_the_ticket_it_names(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """**The cross-endpoint agreement, over HTTP.** A countdown on the analytics dashboard
    and a countdown on the ticket detail screen come from one `resolve_position` call, so
    they cannot disagree.

    `due_at` is asserted exactly: it is `created_at + target`, both of which are fixed.
    `remaining_seconds` is allowed a second of drift, because the two values are read by
    two requests and each measures from its own `now` — asserting equality there would be
    a test that fails roughly once in two hundred runs for a reason that is not a defect.
    """
    ticket = raise_ticket(manager, portal["record"]["id"], priority="high")
    backdate(sync_engine, ticket["id"], minutes=HIGH[0] * 5)

    risk = read(manager, "/sla")["risks"][0]
    detail = manager.get(f"{TICKETS}/{risk['id']}")
    assert detail.status_code == 200, detail.text
    sla = cast("dict[str, Any]", detail.json()["sla"])

    assert sla["policy"]["priority"] == "high"
    assert risk["due_at"] == sla["response"]["due_at"]
    assert abs(risk["remaining_seconds"] - sla["response"]["remaining_seconds"]) <= 1


def test_a_priority_with_no_active_policy_has_no_clock_and_is_absent(
    org: OrgSession, manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """Switch off `urgent` and the ticket stops having a deadline anywhere.

    The join in `analytics_repository` is to the *active* policy, so a switched-off
    priority is absent from the risk ranking, absent from compliance, and absent from the
    overdue count — the same absence `GET /tickets/{id}` reports as a `null` `sla`. A
    ticket counted as breaching against a target nobody is enforcing would be the worst of
    the three possible answers.

    **This is also the one invalidation site that is not a ticket write**, which is why it
    is asserted through a second read: the standing was cached by the first one, and only
    `update_policy`'s own `cache.invalidate` can make the change visible before its TTL.
    """
    ticket = raise_ticket(manager, portal["record"]["id"], priority="urgent")
    backdate(sync_engine, ticket["id"], minutes=600)
    before = read(manager, "/sla")
    assert len(before["risks"]) == 1
    assert before["overdue"] == 1

    switched_off = org.patch(f"{SLA}/policies/urgent", json={"is_active": False})
    assert switched_off.status_code == 200, switched_off.text

    body = read(manager, "/sla")
    assert body["risks"] == []
    assert body["overdue"] == 0
    assert body["open_tickets"] == 1  # still open, and now with no deadline to be late for

    detail = manager.get(f"{TICKETS}/{ticket['id']}")
    assert detail.json()["sla"] is None


def test_a_limit_over_the_risk_cap_is_refused(manager: OrgSession) -> None:
    """50, because the risk list is a ranking rather than a page — there is no `offset`,
    so a caller asking for a hundred is asking for a report this route does not produce."""
    assert status_of(manager, "/sla", limit=51) == 422


# ---------------------------------------------------------------------------
# Row scope
# ---------------------------------------------------------------------------


def test_a_manager_counts_the_organization_and_an_agent_counts_only_theirs(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any]
) -> None:
    """**One endpoint, two answers, and the difference is the row scope.**

    This is `GET /tickets`'s rule applied to aggregates: `AGENT` is `RowScope.ASSIGNED`,
    so an agent's analytics describe their own work and a manager's describe the tenant's.
    Not a second scope map — `TICKET_SCOPE_BY_ROLE`, unchanged since Phase G.

    It is also the API-level half of the cache-scope test: the two callers read the same
    route with the same window, so a cache keyed without the caller's scope would serve
    whichever of them asked first to both — and this assertion is what would notice.
    """
    mine = raise_ticket(manager, portal["record"]["id"])
    raise_ticket(manager, portal["record"]["id"])
    assign(manager, mine["id"], agent)

    manager_totals = read(manager, "/overview")["totals"]
    agent_totals = read(agent, "/overview")["totals"]

    assert manager_totals["total"] == 2
    assert agent_totals["total"] == 1
    assert agent_totals["by_status"]["assigned"] == 1
    assert agent_totals["open"] == 1


def test_an_agent_sees_their_own_risk_list(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any]
) -> None:
    """The scope is in the ranking too, not only in the totals — every aggregate goes
    through the same `_select`, which is the property that makes this list exhaustive
    rather than a spot check."""
    theirs = raise_ticket(manager, portal["record"]["id"], priority="high")
    ours = raise_ticket(manager, portal["record"]["id"], priority="urgent")
    assign(manager, ours["id"], agent)

    assert [risk["id"] for risk in read(manager, "/sla")["risks"]] == [
        ours["id"],
        theirs["id"],
    ]
    assert [risk["id"] for risk in read(agent, "/sla")["risks"]] == [ours["id"]]


@pytest.mark.parametrize("path", PATHS)
def test_a_customer_is_refused_on_every_analytics_route(portal: dict[str, Any], path: str) -> None:
    """**§54's "authorization on every protected resource", one route at a time.**

    A customer holds no analytics capability at all — not `ANALYTICS_OWN` and certainly
    not `ANALYTICS_ORG` — and the parametrization is what stops a sixth route arriving
    with a capability that quietly includes them.
    """
    assert status_of(portal["session"], path) == 403


# ---------------------------------------------------------------------------
# The cache: that the invalidation is wired, not merely implemented
# ---------------------------------------------------------------------------


def test_a_new_ticket_moves_the_next_overview_without_waiting_for_the_ttl(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """Read, write, read again — and the second read includes the new ticket.

    The first read caches the overview for a tenant that has changed nothing. The write
    bumps the version integer, which is what orphans that entry: without the
    `cache.invalidate` in `ticket_service.create_ticket` the second read would still
    answer from the first, with numbers that are correct and five minutes old. Nothing
    else in the suite would fail, which is why this test exists.
    """
    before = read(manager, "/overview")["totals"]["total"]
    raise_ticket(manager, portal["record"]["id"])
    after = read(manager, "/overview")["totals"]["total"]

    assert before == 0
    assert after == 1


def test_a_public_reply_moves_the_next_compliance_reading(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """The response timer stopping is what moves the compliance number, so a *reply*
    invalidates and an internal note does not — the one asymmetry in the seven call
    sites, and the one a later tidy-up is most likely to erase by invalidating from
    `_post` instead of from the two callers."""
    waiting = raise_ticket(manager, portal["record"]["id"], priority="high")
    assert read(manager, "/sla")["compliance"]["response"]["met"] == 0

    reply(manager, waiting["id"])

    assert read(manager, "/sla")["compliance"]["response"]["met"] == 1


def test_an_internal_note_does_not_move_the_compliance_reading(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """The control for the test above: a note never sets `first_response_at`, so nothing
    an aggregate reads has changed and the cached entry is still the truth.

    Asserted through the API rather than by inspecting the version integer, because what
    matters is the number a caller sees, not which key served it.
    """
    waiting = raise_ticket(manager, portal["record"]["id"], priority="high")
    assert read(manager, "/sla")["compliance"]["response"]["met"] == 0

    note = manager.post(
        f"{TICKETS}/{waiting['id']}/notes", json={"body": "Refunded in full, see billing."}
    )
    assert note.status_code == 201, note.text

    assert read(manager, "/sla")["compliance"]["response"]["met"] == 0
    assert read(manager, "/overview")["response_time"]["count"] == 0
