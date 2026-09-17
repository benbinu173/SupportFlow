"""Phase S end to end: five endpoints, one clock, and a cache that cannot leak.

The suites already assert the aggregates against a fixture whose arithmetic they work out,
the cache's fail-open paths against a fake, and the SQL fragment against the pure clock at
every boundary. This script exists for the five things none of those can show:

* **That the numbers hold over a request a person made**, against a fixture built entirely
  through the API and aged with one direct write. Every expectation below is derived — from
  the ages the fixture was given and the targets this tenant's own policies carry — and
  never from a stored constant. A walkthrough that asserted "the total is 9" would only be
  asserting that the script counts the way the script counts.
* **That the two halves of `/analytics/sla` describe what they claim to.** Compliance is
  over the cohort the window names; `overdue` and `open_tickets` are over the queue as it
  stands. Section 5 asks the same endpoint for a one-day window and shows the second pair
  unchanged while the first empties, which is the distinction a reader is most likely to
  get wrong.
* **That a countdown on the dashboard and a countdown on the ticket screen are one number.**
  Read over HTTP from two endpoints, in section 6.
* **That the endpoint ordering is the clock's ordering.** The ranking comes from SQL and
  every value on it from `resolve_position`; the expected order here is rebuilt from each
  ticket's own `created_at` and the policy target, so a wrong comparison operator in the
  fragment shows up as a wrong list rather than as a plausible one.
* **That the cache is invisible and the version integer moves.** The interesting failure in
  this phase is silent: a missing `invalidate` produces perfectly correct numbers that are
  up to a TTL out of date, and nothing else in the project would notice.

Three steps:

    # 1. the API, from backend/
    .venv/Scripts/python.exe -m uvicorn app.main:app \\
        --loop app.core.event_loop:loop_factory --port 8000

    # 2. this script, from backend/
    .venv/Scripts/python.exe scripts/phase_s_walkthrough.py

    # 3. afterwards, with the API still running. Section 7 sets this up and cannot perform
    #    it itself: stopping Redis would stop it for the script too.
    #      docker compose stop redis
    #      read GET /api/v1/analytics/overview again, with the token from step 2's login
    #      docker compose start redis
    #    It must answer, correctly and a little slower. That is the fail-open path.

Three things to know before running it. It **registers two organizations**, and §45 limits
registration to five an hour per address, so a third run inside the hour reports `429`
rather than a failed assertion — the limiter working, not the phase:

    docker compose exec redis redis-cli -n 0 --scan --pattern 'ratelimit:register:*'

It **writes to `tickets.created_at`**, which is the only way to age a clock without waiting
half an hour for it and which no endpoint exposes. Point it at a development database. And
it leaves its organizations behind: nothing here deletes anything.
"""

# ruff: noqa: T201

import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import redis as redis_sync
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import engine
from app.core.event_loop import run

BASE = "http://localhost:8000/api/v1"
TICKETS = f"{BASE}/tickets"
ANALYTICS = f"{BASE}/analytics"
PASSWORD = "correct-horse-battery-staple"

#: §27's URGENT row as seeded: thirty minutes to a first response. Transcribed from the
#: specification table rather than imported from `sla_service.DEFAULT_POLICIES`, for the
#: reason the Phase Q and R scripts give — a walkthrough that derives its expectation from
#: the code under test shows only that the code is self-consistent. The *actual* target is
#: read back from the tenant's own policies below, because it is editable per organization.
URGENT_RESPONSE_MINUTES = 30

#: HIGH's two targets, transcribed for the same reason. Only used to choose the fixture's
#: ages, never to assert a number the API returned.
HIGH_RESPONSE_MINUTES = 120
HIGH_RESOLUTION_MINUTES = 480

#: How old to make the ticket that should be sitting in URGENT's warning band. 26 of 30
#: minutes is inside a default 80% threshold (24) and comfortably short of the target, so
#: the ticket is in the band without being within a minute of either edge of it — the wall
#: clock advances during this script, and a fixture parked on a boundary would be flaky
#: rather than exact. Whether it *is* warning is computed from the tenant's own threshold.
URGENT_WARNING_AGE_MINUTES = 26

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. The script runs to the end even after a failure.

    A walkthrough that stops at the first problem hides the rest of the story, and seeing
    the rest of the story is why one walks through rather than running the suite — which
    already stops at the first problem.
    """
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ok    {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}{f' -- {detail}' if detail else ''}")


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def when(value: str) -> datetime:
    """A timestamp the API produced, as an aware `datetime`.

    Pydantic renders an aware `datetime` with a trailing `Z`, which `fromisoformat` has
    accepted since 3.11. Nothing here parses a date this API did not serialize.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read(session: "Api", path: str, **params: Any) -> dict[str, Any]:
    response = session.get(f"{ANALYTICS}{path}", params=params or None)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    return dict(response.json())


def status_of(session: "Api", path: str, **params: Any) -> int:
    return session.get(f"{ANALYTICS}{path}", params=params or None).status_code


class Api:
    """One signed-in caller: an HTTP client carrying a bearer token."""

    def __init__(self, token: str, email: str) -> None:
        self.token = token
        self.email = email
        self._client = httpx.Client(base_url=BASE, timeout=30.0)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._user_id: str | None = None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.request(method, path, headers=self._headers, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    @property
    def user_id(self) -> str:
        """This caller's own id, from `/auth/me`.

        Asked for rather than passed in: `TokenResponse` carries the access token and
        nothing else, so a session's identity is only knowable by asking. Reading it from
        the endpoint a client would use keeps the scope assertions below about the API
        rather than about this script.
        """
        if self._user_id is None:
            response = self.get("/auth/me")
            response.raise_for_status()
            self._user_id = str(response.json()["id"])
        return self._user_id


class Tenant:
    """A registered organization, with its founding admin's session."""

    def __init__(self, label: str) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.name = f"{label} {suffix}"
        self.email = f"admin-{suffix}@walkthrough.example"

        response = httpx.post(
            f"{BASE}/auth/register",
            json={
                "organization_name": self.name,
                "name": "Admin",
                "email": self.email,
                "password": PASSWORD,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        self.admin = Api(response.json()["access_token"], self.email)

    def member(self, role: str, **extra: Any) -> Api:
        """Create a user in this organization, and return their signed-in session."""
        email = f"member-{uuid.uuid4().hex[:8]}@walkthrough.example"
        created = self.admin.post(
            "/users",
            json={
                "name": role.title(),
                "email": email,
                "password": PASSWORD,
                "role": role,
                **extra,
            },
        )
        created.raise_for_status()

        logged_in = httpx.post(
            f"{BASE}/auth/login", json={"email": email, "password": PASSWORD}, timeout=30.0
        )
        logged_in.raise_for_status()
        return Api(logged_in.json()["access_token"], email)

    def customer(self, name: str) -> str:
        response = self.admin.post(
            "/customers", json={"name": name, "email": f"{uuid.uuid4().hex[:8]}@customer.example"}
        )
        response.raise_for_status()
        return str(response.json()["id"])

    def ticket_for(self, customer_id: str, subject: str, *, priority: str) -> dict[str, Any]:
        response = self.admin.post(
            "/tickets",
            json={
                "subject": subject,
                "description": "It does not work.",
                "customer_id": customer_id,
                "priority": priority,
            },
        )
        response.raise_for_status()
        return dict(response.json())

    def move(self, ticket_id: str, status: str) -> None:
        response = self.admin.post(f"{TICKETS}/{ticket_id}/status", json={"status": status})
        response.raise_for_status()

    def assign(self, ticket_id: str, agent_id: str) -> None:
        response = self.admin.post(
            f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": agent_id}
        )
        response.raise_for_status()

    def reply(self, ticket_id: str) -> None:
        """A public staff reply, which is what stops the response clock."""
        response = self.admin.post(f"{TICKETS}/{ticket_id}/messages", json={"body": "Looking."})
        response.raise_for_status()

    def targets(self) -> dict[str, dict[str, int]]:
        """This organization's SLA targets, by priority.

        Read rather than assumed: every field is editable per tenant, so a walkthrough that
        hardcoded 80 for the warning threshold would fail the day somebody set it to 50.
        The expectations in sections 4 to 6 are all computed from this map.
        """
        response = self.admin.get("/sla/policies")
        response.raise_for_status()
        return {
            row["priority"]: {
                "response": int(row["response_time_minutes"]),
                "resolution": int(row["resolution_time_minutes"]),
                "threshold": int(row["warning_threshold_percent"]),
            }
            for row in response.json()
        }


@dataclass(frozen=True)
class Plan:
    """One ticket in the fixture, and everything that is done to it.

    The fixture is data rather than a series of calls so that the expectations can be
    *derived* from it. This table is the only place a number about the fixture is written
    down; every assertion about the API is computed from this table and from the policies
    the tenant reports.
    """

    key: str
    priority: str
    age_minutes: int
    #: `"agent"` to assign it to section 2's agent, or `None` to leave it unassigned.
    who: str | None = None
    #: A public reply, which stops the response timer.
    reply: bool = False
    #: Walked to `RESOLVED`, which stops the resolution timer and leaves the queue.
    resolve: bool = False


#: The fixture. The ages are deliberately minutes away from every target rather than on it:
#: a walkthrough's clock is the wall clock and moves while the script runs, so a boundary
#: case here would be flaky. `tests/integration/test_analytics_sla_agreement.py` is where
#: the boundaries are asserted, against a fixed `now`.
FIXTURE: tuple[Plan, ...] = (
    # Response timers stopped, two inside HIGH's two hours and one well past it.
    Plan("met_one", "high", 45, who="agent", reply=True),
    Plan("met_two", "high", 90, reply=True),
    Plan("late", "high", HIGH_RESPONSE_MINUTES * 3, reply=True),
    # Resolution timers stopped, one immediately and one long after its eight hours.
    Plan("resolved_in_time", "high", 0, who="agent", resolve=True),
    Plan("resolved_late", "high", HIGH_RESOLUTION_MINUTES + 120, who="agent", resolve=True),
    # Untouched, and the only two carrying a live countdown: one past URGENT's thirty
    # minutes, one inside its warning band. These two decide `overdue`.
    Plan("urgent_breached", "urgent", URGENT_RESPONSE_MINUTES * 2),
    Plan("urgent_warning", "urgent", URGENT_WARNING_AGE_MINUTES),
    Plan("med_open", "medium", 60),
    Plan("low_open", "low", 0),
)


def quiet_engine() -> None:
    """Silence the statements *this* process issues.

    The engine echoes every statement when `DEBUG` is on, which is right for the server and
    unreadable in the middle of this output. Only this process is affected — the API logs in
    its own — so nothing here hides what the server would have logged. Idempotent, and
    called before each direct read or write rather than once in `main`, so the setting
    travels with the code that needs it.
    """
    engine.echo = False


def age_the_ticket(ticket_id: str, *, minutes: int) -> None:
    """Move a ticket's clock back, which no endpoint exposes.

    The same admission the Phase Q and R walkthroughs make: the only way to test a deadline
    without waiting for it is to move the row the deadline is computed from, and that means
    a direct write against a development database. `created_at` and not `updated_at`,
    because `sla_service` starts both timers there — and the two stop columns are left
    alone, so the interval between creation and a stop is exactly the number of minutes
    passed here.
    """
    statement = text(
        "UPDATE tickets SET created_at = created_at - make_interval(mins => :minutes) "
        "WHERE id = CAST(:id AS uuid)"
    )
    quiet_engine()

    async def write() -> None:
        async with engine.begin() as connection:
            await connection.execute(statement, {"minutes": minutes, "id": ticket_id})

    run(write())


def organization_of(user_id: str) -> str:
    """The tenant a session belongs to, read from the database.

    Nothing in the API's responses carries an organization id — the identity is derived
    from the token and never echoed — so a script that wants to look up a cache key has to
    ask the table. `tests/security/test_analytics_isolation.py` does the same, for the same
    reason.
    """
    quiet_engine()

    # The async engine, driven by `run`, like every other direct query here: `sync_engine`
    # is a handle for the *dialect's* internals and cannot be used from a frame with no
    # greenlet — psycopg's driver is async either way.
    async def read_one() -> str:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT organization_id FROM users WHERE id = CAST(:id AS uuid)"),
                {"id": user_id},
            )
            return str(result.scalar_one())

    return run(read_one())


#: A real client against the real server, as `tests/security/test_analytics_isolation.py`
#: uses — the property under test is what the application *actually stored*, not what this
#: script believes the key format to be. Synchronous, because the script's own frame has no
#: loop and `app.core.redis`'s client belongs to the API process's loop, not to this one.
def _redis() -> redis_sync.Redis:
    return redis_sync.Redis.from_url(str(get_settings().REDIS_URL))


def version_in_redis(organization_id: str) -> int:
    client = _redis()
    try:
        raw = client.get(f"analytics:version:{organization_id}")
    finally:
        client.close()
    return int(raw) if raw is not None else 0


def keys_in_redis(pattern: str) -> list[str]:
    client = _redis()
    try:
        return sorted(str(key.decode()) for key in client.keys(pattern))
    finally:
        client.close()


def expiry_of(key: str) -> int:
    client = _redis()
    try:
        return int(client.ttl(key))
    finally:
        client.close()


# ---------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------


def the_window_is_resolved_echoed_and_refused(tenant: Tenant) -> None:
    """The range parameters, and every way of spelling one that is refused."""
    section("1. The window is resolved, echoed back, and refused when it is nonsense")

    window = read(tenant.admin, "/overview")["range"]
    start, end = when(window["start"]), when(window["end"])
    now = datetime.now(UTC)

    check("the bucket is a UTC day", window["bucket"] == "day", str(window["bucket"]))
    check(
        "an unasked-for range defaults to the thirty days before now",
        timedelta(days=30) <= now - start <= timedelta(days=30, seconds=10),
        f"start is {now - start} old",
    )
    check("and its upper bound is now", abs(now - end) <= timedelta(seconds=10))

    chosen_start = now - timedelta(days=7)
    chosen_end = now - timedelta(days=1)
    echoed = read(
        tenant.admin, "/overview", start=chosen_start.isoformat(), end=chosen_end.isoformat()
    )["range"]
    check(
        "an explicit window comes back exactly as it was asked for",
        when(echoed["start"]) == chosen_start and when(echoed["end"]) == chosen_end,
        f"{echoed['start']} .. {echoed['end']}",
    )

    check(
        "a start after its end is refused",
        status_of(
            tenant.admin,
            "/overview",
            start=now.isoformat(),
            end=(now - timedelta(days=1)).isoformat(),
        )
        == 422,
    )
    check(
        "a bound with no timezone is refused",
        status_of(tenant.admin, "/overview", start="2026-01-01T00:00:00") == 422,
    )
    check(
        "a window longer than a year is refused rather than truncated",
        status_of(tenant.admin, "/overview", start=(now - timedelta(days=400)).isoformat()) == 422,
    )
    check("a risk limit over the cap is refused", status_of(tenant.admin, "/sla", limit=51) == 422)
    check(
        "an agent-page limit over the cap is refused",
        status_of(tenant.admin, "/agents", limit=101) == 422,
    )


def build_the_house(tenant: Tenant) -> dict[str, Any]:
    """Build the fixture through the API, and hand back what it was made of."""
    section("2. A house with a known shape, built through the API")

    customer = tenant.customer("Ada Lovelace")
    agent = tenant.member("agent", name="Ana")

    tickets: dict[str, dict[str, Any]] = {}
    for plan in FIXTURE:
        ticket = tenant.ticket_for(customer, plan.key, priority=plan.priority)
        tickets[plan.key] = ticket
        # The order is the point: age first, so the reply or the resolution lands *after*
        # the moved creation and the interval is the age this table chose.
        if plan.age_minutes:
            age_the_ticket(ticket["id"], minutes=plan.age_minutes)
        if plan.who == "agent":
            tenant.assign(ticket["id"], agent.user_id)
        if plan.reply:
            tenant.reply(ticket["id"])
        if plan.resolve:
            tenant.move(ticket["id"], "in_progress")
            tenant.move(ticket["id"], "resolved")

    print(f"  ..    {len(FIXTURE)} tickets, as:")
    for plan in FIXTURE:
        made = ", ".join(
            filter(
                None,
                [
                    f"{plan.age_minutes} min old" if plan.age_minutes else None,
                    "assigned" if plan.who else "unassigned",
                    "replied" if plan.reply else None,
                    "resolved" if plan.resolve else None,
                ],
            )
        )
        print(f"          {plan.key:<18} {plan.priority:<7} {made}")

    return {"customer": customer, "agent": agent, "tickets": tickets}


def the_endpoints_count_what_the_house_contains(tenant: Tenant, house: dict[str, Any]) -> None:
    """Totals, the three breakdowns, and the AI block, against the fixture's own size."""
    section("3. Every endpoint counts what the house contains, and nothing else")

    total = len(FIXTURE)
    overview = read(tenant.admin, "/overview")
    totals = overview["totals"]

    check("the total is the number of tickets the fixture raised", totals["total"] == total)

    # Derived from the lifecycle the fixture walked: everything starts OPEN, an assignment
    # makes it ASSIGNED, and `resolve` walks the last two edges.
    expected_status = dict.fromkeys(
        ("open", "assigned", "in_progress", "waiting_for_customer", "resolved", "closed"), 0
    )
    for plan in FIXTURE:
        if plan.resolve:
            expected_status["resolved"] += 1
        elif plan.who:
            expected_status["assigned"] += 1
        else:
            expected_status["open"] += 1

    check(
        "every status is reported, including the ones nothing is in",
        totals["by_status"] == expected_status,
        f"got {totals['by_status']}",
    )
    check(
        "the status breakdown sums to the total it is a breakdown of",
        sum(totals["by_status"].values()) == totals["total"],
    )
    check(
        "open is every status that is not terminal",
        totals["open"] == total - totals["resolved"] - totals["closed"],
    )

    volume = overview["volume"]
    check(
        "the volume series adds up to the total",
        sum(point["count"] for point in volume) == total,
        f"{len(volume)} buckets summing to {sum(p['count'] for p in volume)}",
    )
    check(
        "the points are in ascending order, which is what a chart plots",
        [when(point["bucket"]) for point in volume]
        == sorted(when(point["bucket"]) for point in volume),
    )

    breakdown = read(tenant.admin, "/tickets")
    by_priority = {row["priority"]: row["count"] for row in breakdown["by_priority"]}
    expected_priority = dict.fromkeys(("low", "medium", "high", "urgent"), 0)
    for plan in FIXTURE:
        expected_priority[plan.priority] += 1
    check(
        "every priority is a series, whether or not the fixture used it",
        by_priority == expected_priority,
        f"got {by_priority}",
    )

    categories = breakdown["by_category"]
    check(
        "the category breakdown is one unclassified bucket until Phase U classifies",
        len(categories) == 1
        and categories[0]["category"] is None
        and categories[0]["count"] == total,
        str(categories),
    )

    sentiment = read(tenant.admin, "/sentiment")
    # The `None` bucket first and then every `Sentiment` member, zeros included — the same
    # completeness `by_status` has, and the reason `unanalysed` is a visible number rather
    # than the residue of a distribution over classified tickets only.
    expected_buckets = [{"sentiment": None, "count": total}] + [
        {"sentiment": value, "count": 0} for value in ("positive", "neutral", "negative")
    ]
    check(
        "the sentiment distribution names the unanalysed bucket and every real one",
        sentiment["buckets"] == expected_buckets,
        str(sentiment["buckets"]),
    )
    check(
        "so unanalysed is every ticket, and analysed is none of them",
        sentiment["unanalysed"] == total and sentiment["analysed"] == 0,
        f"{sentiment['unanalysed']} of {total} unanalysed",
    )
    check(
        "and the buckets account for every ticket exactly once",
        sum(bucket["count"] for bucket in sentiment["buckets"]) == total,
    )

    usage = overview["ai_usage"]
    check(
        "AI usage is a real zero over a table nothing writes to yet",
        usage["calls"] == 0
        and usage["failed_calls"] == 0
        and usage["prompt_tokens"] == 0
        and Decimal(usage["cost_usd"]) == Decimal(0)
        and usage["by_operation"] == [],
        str(usage),
    )


def the_averages_are_the_intervals_the_timestamps_imply(
    tenant: Tenant, house: dict[str, Any]
) -> None:
    """Both averages, against the ages the fixture chose.

    A second of slack, because the two values are read by two requests: the ticket was
    created, aged, and then answered, so the reply lands a round trip after the creation it
    is measured against. Asserting exact equality would be a walkthrough that fails for a
    reason that is not a defect.
    """
    section("4. The averages are the intervals the timestamps imply")

    overview = read(tenant.admin, "/overview")
    replied = [plan for plan in FIXTURE if plan.reply]
    resolved = [plan for plan in FIXTURE if plan.resolve]

    response = overview["response_time"]
    expected_response = sum(plan.age_minutes for plan in replied) * 60 / len(replied)
    check(
        "the response average is the mean of the intervals the fixture planted",
        abs(response["average_seconds"] - expected_response) <= 10,
        f"got {response['average_seconds']}, expected about {expected_response:.0f}",
    )
    check("over exactly the tickets that were answered", response["count"] == len(replied))

    resolution = overview["resolution_time"]
    expected_resolution = sum(plan.age_minutes for plan in resolved) * 60 / len(resolved)
    check(
        "the resolution average is the mean of creation to resolution",
        abs(resolution["average_seconds"] - expected_resolution) <= 10,
        f"got {resolution['average_seconds']}, expected about {expected_resolution:.0f}",
    )
    check("over exactly the tickets that were resolved", resolution["count"] == len(resolved))


def the_sla_reading_is_a_cohort_and_a_queue(tenant: Tenant, targets: dict[str, Any]) -> None:
    """Compliance over the window, the counts over the queue — and the difference shown."""
    section("5. Compliance describes the cohort; the past-due count describes the queue")

    body = read(tenant.admin, "/sla", limit=50)
    compliance = body["compliance"]

    # The fixture's arithmetic, not a second clock: "the age I gave this ticket, against the
    # target this tenant's own policy carries". No warning band, no `LEAST`, no timer
    # precedence — which is exactly why it is a usable check on the code that has them.
    def stopped(timer: str) -> tuple[list[str], list[str]]:
        met, breached = [], []
        for plan in FIXTURE:
            if not (plan.reply if timer == "response" else plan.resolve):
                continue
            target = targets[plan.priority][timer]
            (met if plan.age_minutes < target else breached).append(plan.key)
        return met, breached

    for timer in ("response", "resolution"):
        met, breached = stopped(timer)
        row = compliance[timer]
        check(
            f"the {timer} timer's record is the met-over-stopped ratio the fixture makes",
            (row["met"], row["breached"]) == (len(met), len(breached)),
            f"got {row['met']}/{row['breached']}, expected {len(met)}/{len(breached)}",
        )
        check(
            f"the {timer} rate is derived from those two counts",
            row["rate"] == len(met) / (len(met) + len(breached)),
            f"got {row['rate']}",
        )

    pooled_met = sum(compliance[timer]["met"] for timer in ("response", "resolution"))
    pooled_breached = sum(compliance[timer]["breached"] for timer in ("response", "resolution"))
    check(
        "the pooled rate counts both timers' tickets rather than averaging their rates",
        compliance["met"] == pooled_met
        and compliance["breached"] == pooled_breached
        and compliance["rate"] == pooled_met / (pooled_met + pooled_breached),
        f"got {compliance['met']}/{compliance['breached']} at rate {compliance['rate']}",
    )

    terminal = [plan for plan in FIXTURE if plan.resolve]
    check(
        "open_tickets is the non-terminal queue, which is the denominator it reads as",
        body["open_tickets"] == len(FIXTURE) - len(terminal),
        f"got {body['open_tickets']}",
    )

    # The nearest outstanding deadline already in the past. Terminal tickets are off the
    # queue entirely, and a stopped timer contributes nothing to the comparison.
    expected_overdue = [
        plan.key
        for plan in FIXTURE
        if not plan.resolve
        and plan.age_minutes >= targets[plan.priority]["resolution" if plan.reply else "response"]
    ]
    check(
        "the past-due count is the fixture's own overdue tickets",
        body["overdue"] == len(expected_overdue),
        f"got {body['overdue']}, expected {expected_overdue}",
    )
    check(
        "the count and the ranking agree, which is the differential property read live",
        body["overdue"] == len([risk for risk in body["risks"] if risk["remaining_seconds"] <= 0]),
        f"count {body['overdue']}, ranking "
        f"{[r['subject'] for r in body['risks'] if r['remaining_seconds'] <= 0]}",
    )

    # **The distinction the docstring claims.** A window that closed before any of these
    # tickets existed empties the cohort and leaves the queue untouched: compliance has
    # nothing stopped to describe, while the past-due ticket is still past due. Hiding the
    # worst tickets because they fell outside a date range would be the worst omission a
    # dashboard could make.
    later = datetime.now(UTC)
    empty = read(
        tenant.admin,
        "/sla",
        start=(later + timedelta(days=1)).isoformat(),
        end=(later + timedelta(days=2)).isoformat(),
    )
    check(
        "a window with no tickets in it has no compliance to report, and says so",
        empty["compliance"]["rate"] is None
        and empty["compliance"]["response"]["met"] == 0
        and empty["compliance"]["response"]["breached"] == 0,
        str(empty["compliance"]),
    )
    check(
        "while the queue behind it is exactly the same",
        empty["overdue"] == body["overdue"] and empty["open_tickets"] == body["open_tickets"],
        f"overdue {empty['overdue']} and open {empty['open_tickets']} in both",
    )


def the_ranking_is_the_clocks(
    tenant: Tenant, house: dict[str, Any], targets: dict[str, Any]
) -> None:
    """The risk list, rebuilt from each ticket's own creation instant."""
    section("6. The ranking is the clock's, and its rows are its tickets'")

    tickets: dict[str, dict[str, Any]] = house["tickets"]
    risks = read(tenant.admin, "/sla", limit=50)["risks"]
    key_of = {ticket["id"]: key for key, ticket in tickets.items()}

    # The expected order: every non-terminal ticket's *binding* deadline — the resolution
    # timer once a reply has stopped the response one, the response timer otherwise — read
    # from the ticket's own `created_at` rather than from anything this script remembered.
    expected: list[tuple[datetime, str]] = []
    for plan in FIXTURE:
        if plan.resolve:
            # Terminal, and `risk_candidates` excludes the terminal statuses outright.
            continue
        detail = tenant.admin.get(f"{TICKETS}/{tickets[plan.key]['id']}")
        detail.raise_for_status()
        timer = "resolution" if plan.reply else "response"
        due = when(detail.json()["created_at"]) + timedelta(minutes=targets[plan.priority][timer])
        expected.append((due, plan.key))
    expected.sort(key=lambda row: row[0])

    check(
        "the list is ranked by the soonest outstanding deadline, worst first",
        [key_of.get(risk["id"]) for risk in risks] == [key for _, key in expected],
        f"got {[key_of.get(r['id']) for r in risks]}, expected {[k for _, k in expected]}",
    )
    remaining = [risk["remaining_seconds"] for risk in risks]
    check("and the remaining times ascend with it", remaining == sorted(remaining), str(remaining))
    check(
        "the worst ticket is the one the fixture made past due",
        risks[0]["remaining_seconds"] < 0
        and key_of[risks[0]["id"]]
        in {plan.key for plan in FIXTURE if plan.age_minutes >= targets[plan.priority]["response"]},
    )

    # The warning band is the one state SQL deliberately cannot express, so it is asserted
    # against the tenant's own threshold rather than a transcribed 80. A missing row is
    # reported as a failure rather than raised as a `StopIteration`, because this script's
    # job is to keep going and show the rest of the story.
    urgent = targets["urgent"]
    warning_at = urgent["response"] * urgent["threshold"] // 100
    band = [risk for risk in risks if key_of.get(risk["id"]) == "urgent_warning"]
    check(
        "the ticket inside the warning band is on the list at all",
        len(band) == 1,
        f"found {len(band)}",
    )
    if band:
        check(
            "and is reported as warning rather than as past due",
            band[0]["state"]
            == ("warning" if warning_at <= URGENT_WARNING_AGE_MINUTES else "on_track")
            and 0 < band[0]["remaining_seconds"] <= urgent["response"] * 60,
            f"state {band[0]['state']}, {band[0]['remaining_seconds']}s left of a "
            f"{urgent['response']}-minute target warning at {warning_at}",
        )

    # **The cross-endpoint agreement.** A countdown on the dashboard and a countdown on the
    # ticket screen come from one `resolve_position` call, so they cannot disagree. `due_at`
    # is asserted exactly — it is `created_at + target`, both fixed — and `remaining_seconds`
    # within a second, because the two values are read by two requests that each measure
    # from their own `now`.
    first = risks[0]
    detail = tenant.admin.get(f"{TICKETS}/{first['id']}")
    detail.raise_for_status()
    on_the_ticket = detail.json()["sla"][first["timer"]]
    check(
        "the same ticket reports the same deadline on the detail screen",
        on_the_ticket["due_at"] == first["due_at"],
        f"{first['due_at']} here, {on_the_ticket['due_at']} there",
    )
    check(
        "and the same countdown, to within the round trip",
        abs(on_the_ticket["remaining_seconds"] - first["remaining_seconds"]) <= 1,
        f"{first['remaining_seconds']}s here, {on_the_ticket['remaining_seconds']}s there",
    )

    # The agent's own risk list is their assigned work and nobody else's — the row-scope
    # boundary that no cross-tenant test can see.
    agent = house["agent"]
    theirs = read(agent, "/sla", limit=50)["risks"]
    check(
        "an agent's risk list contains only tickets assigned to them",
        theirs
        and all(
            key_of[risk["id"]] in {plan.key for plan in FIXTURE if plan.who == "agent"}
            for risk in theirs
        ),
        str([key_of.get(risk["id"]) for risk in theirs]),
    )


def the_cache_is_invisible_and_the_version_moves(tenant: Tenant, house: dict[str, Any]) -> None:
    """A hit, a miss caused by a write, and the version integer that causes it.

    The request carries an **explicit** window, and that is not incidental: with the range
    defaulted, `end` is the instant of each request, so two reads are two different
    questions and the second is correctly a miss. A walkthrough that checked the cache with
    defaulted bounds would be asserting that two different keys hold equal numbers.

    The key is found by taking the keyspace before and after, rather than by asserting the
    keyspace holds one key: the sections above have already cached a dozen entries under
    defaulted windows of their own, and every one of them is a different question. The
    difference between two snapshots is this read and only this read.
    """
    section("7. The cache is invisible, and a write moves the version under it")

    now = datetime.now(UTC)
    window = {
        "start": (now - timedelta(days=30)).isoformat(),
        "end": (now + timedelta(days=1)).isoformat(),
    }
    organization = organization_of(tenant.admin.user_id)
    house["organization"] = organization
    prefix = f"analytics:overview:{organization}:org:"

    before_keys = set(keys_in_redis(f"{prefix}*"))
    before = read(tenant.admin, "/overview", **window)
    added = set(keys_in_redis(f"{prefix}*")) - before_keys

    check(
        "the answer is cached under a key carrying the tenant, the row scope and the version",
        len(added) == 1 and next(iter(added)).startswith(prefix) and ":org:" in next(iter(added)),
        str(added),
    )
    if len(added) != 1:
        return
    key = next(iter(added))
    ttl_before = expiry_of(key)

    # Longer than the whole-second resolution of a Redis TTL, so a re-`SET` would be visible
    # as a reset rather than as a coincidence.
    time.sleep(1.2)

    again = read(tenant.admin, "/overview", **window)
    check(
        "reading it again returns exactly the same body, window and all",
        again == before,
        f"{before['range']} became {again['range']}",
    )

    # **The witness for a hit.** `read_through` only writes on a miss, and every write
    # re-`SET`s with the full TTL. A TTL that only counts down therefore proves the entry
    # was read rather than recomputed and stored again — body equality alone would not,
    # because a recompute would produce the same body.
    ttl_after = expiry_of(key)
    check(
        "and it came from Redis rather than from the database",
        0 <= ttl_after < ttl_before,
        f"ttl {ttl_before} -> {ttl_after}",
    )

    version_before = version_in_redis(organization)
    check(
        "the fixture's own writes have already created this tenant's version key",
        version_before > 0,
        f"version {version_before}",
    )

    tenant.ticket_for(house["customer"], "cache_buster", priority="low")
    version_after = version_in_redis(organization)
    check(
        "and one more ticket write moves it forward by exactly one",
        version_after == version_before + 1,
        f"{version_before} -> {version_after}",
    )

    after = read(tenant.admin, "/overview", **window)
    check(
        "so the next reading includes the new ticket without waiting for the TTL",
        after["totals"]["total"] == before["totals"]["total"] + 1,
        f"{before['totals']['total']} -> {after['totals']['total']}",
    )
    check(
        "and it was answered under the new version",
        any(f":{version_after}:" in candidate for candidate in keys_in_redis(f"{prefix}*")),
        str(keys_in_redis(f"{prefix}*")),
    )
    check(
        "while the entry it replaced is orphaned rather than deleted — there is no SCAN",
        key in set(keys_in_redis(f"{prefix}*")),
    )

    print(
        "  ..    the fail-open path is step 3 of this script's header: stop Redis with the\n"
        "        API still running, read /analytics/overview again, and it must answer\n"
        "        correctly. A script cannot perform it, because it would stop its own Redis."
    )


def the_boundaries_hold_across_tenants_and_roles(tenant: Tenant, house: dict[str, Any]) -> None:
    """§54's authorization checks, over HTTP, on the aggregates."""
    section("8. Two tenants, two roles, and a portal caller who is refused everywhere")

    other = Tenant("Elsewhere")
    other_customer = other.customer("Grace Hopper")
    for index in range(3):
        other.ticket_for(other_customer, f"theirs-{index}", priority="low")

    ours = read(tenant.admin, "/overview")["totals"]["total"]
    theirs = read(other.admin, "/overview")["totals"]["total"]
    check(
        "each tenant counts its own work and neither counts the other's",
        ours == len(FIXTURE) + 1 and theirs == 3,
        f"{ours} here, {theirs} there",
    )

    agent = house["agent"]
    agent_total = read(agent, "/overview")["totals"]["total"]
    assigned = len([plan for plan in FIXTURE if plan.who == "agent"])
    check(
        "and inside one tenant, an agent counts only what is assigned to them",
        agent_total == assigned and agent_total < ours,
        f"agent {agent_total} of {assigned} assigned, admin {ours}",
    )

    # The second isolation boundary, read in Redis rather than through the numbers: two
    # callers with different reach cannot share an entry by construction. The agent asked
    # `/overview` once, above, so exactly one key exists under their own scope token — a
    # keyed-only-on-tenant cache would have none, and the admin's entry would be what they
    # were served.
    organization = house["organization"]
    agent_keys = keys_in_redis(f"analytics:overview:{organization}:user:{agent.user_id}:*")
    check(
        "the two roles in this tenant are cached under two different scopes",
        len(agent_keys) == 1,
        str(agent_keys),
    )

    check(
        "an agent is refused the per-agent breakdown, which is a comparison between people",
        status_of(agent, "/agents") == 403,
    )
    check(
        "and an admin is not",
        status_of(tenant.admin, "/agents") == 200,
    )

    portal = tenant.member("customer", name="Grace", customer_id=house["customer"])
    refused = {
        path: status_of(portal, path)
        for path in ("/overview", "/tickets", "/agents", "/sla", "/sentiment")
    }
    check(
        "a customer holds no analytics capability, so every one of the five refuses them",
        set(refused.values()) == {403},
        str(refused),
    )
    check(
        "and is refused before a key is built, so no entry exists under a customer's scope",
        keys_in_redis("analytics:*:customer:*") == [],
    )


def main() -> None:
    tenant = Tenant("Analytics")
    the_window_is_resolved_echoed_and_refused(tenant)

    house = build_the_house(tenant)
    targets = tenant.targets()

    the_endpoints_count_what_the_house_contains(tenant, house)
    the_averages_are_the_intervals_the_timestamps_imply(tenant, house)
    the_sla_reading_is_a_cohort_and_a_queue(tenant, targets)
    the_ranking_is_the_clocks(tenant, house, targets)
    the_cache_is_invisible_and_the_version_moves(tenant, house)
    the_boundaries_hold_across_tenants_and_roles(tenant, house)

    print(f"\n{_passed} passed, {_failed} failed")
    print(
        "\nTwo things this script could not do for itself.\n"
        "\n"
        "1. The fail-open path. With the API still running:\n"
        "     docker compose stop redis\n"
        "   then read /api/v1/analytics/overview again -- from http://localhost:8000/docs,\n"
        "   signing in as the admin this run registered, or with a token from\n"
        "   POST /auth/login. It must answer, correctly and a little slower, and the API's\n"
        "   stdout must carry `analytics_cache_unavailable` with an error *type* and no\n"
        "   message -- a connection error's message embeds REDIS_URL, which carries a\n"
        "   password in production. Then `docker compose start redis`.\n"
        "\n"
        "   No credential is printed here on purpose. A walkthrough that echoed a token\n"
        "   would be putting one on a log line, which is the thing Phase R chose the auth\n"
        "   frame to avoid and `tests/security/test_log_hygiene.py` cannot see.\n"
        "\n"
        "2. The differential test, deliberately broken. It is already done and recorded in\n"
        "   the phase notes: three separate comparisons in analytics_repository.py were each\n"
        "   inverted in turn, and tests/integration/test_analytics_sla_agreement.py failed\n"
        "   on the comparison it pins and on no other.\n"
        "\n"
        f"Left behind: two organizations ({tenant.name}, and one named Elsewhere), their\n"
        "tickets, and their cache entries. Nothing here deletes anything."
    )
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError as exc:
        print(
            f"\nCould not reach {exc.request.url}. This walkthrough needs the API:\n"
            "  .venv/Scripts/python.exe -m uvicorn app.main:app "
            "--loop app.core.event_loop:loop_factory --port 8000",
            file=sys.stderr,
        )
        sys.exit(2)
