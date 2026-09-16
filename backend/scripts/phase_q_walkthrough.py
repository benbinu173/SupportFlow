"""Phase Q end to end: a clock derived from columns, and a sweep that runs on a schedule.

The suite already asserts the pure clock, the policy routes, and the sweep's judgement,
through `TestClient` and a database. This script exists for the four things a test cannot
show:

* **That the deadline arithmetic is right against a real clock.** The tests backdate
  `created_at` and then assert; here a ticket is aged in PostgreSQL and read back through
  a running API, so `due_at`, `remaining_seconds` and the timer's state are compared
  against the wall clock of a separate process.
* **That beat's task actually sweeps.** `check_organization_sla` is invoked directly —
  the same function `check_sla_deadlines` dispatches and the same one beat fires — against
  a live database, and the timeline entries and notification rows are read back.
* **That the alert is idempotent across two runs**, which is the at-most-four property:
  two timers, two states, each behind its own guard read off the ticket's own timeline.
  Run the sweep twice and the second run must be silent.
* **That the email leaves the process.** The worker picks up a real task from a real
  Redis, opens a real SMTP connection to Mailpit, and the message is read back out of
  Mailpit's HTTP API. The chain beat → broker → worker → SMTP → Mailpit has no seam.

Four steps, and the middle two are this phase's.

    # 1. the API, from backend/
    .venv/Scripts/python.exe -m uvicorn app.main:app \\
        --loop app.core.event_loop:loop_factory --port 8000

    # 2. the worker, from backend/ — note `-Q`, which is Phase Q's own trap
    .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker \\
        --loglevel=info --pool=solo -Q notifications,sla

    # 3. beat, from backend/ — one process, never two
    .venv/Scripts/python.exe -m celery -A app.workers.celery_app beat \\
        --loglevel=info -s /tmp/celerybeat-schedule

    # 4. this script, from backend/
    .venv/Scripts/python.exe scripts/phase_q_walkthrough.py

`--pool=solo` on Windows, not `prefork`: the default pool forks, and fork is not available
on Windows. `solo` runs one task at a time in the main process, which is what makes
`app/core/event_loop.py`'s `run()` usable from a task at all (ADR-011).

Three things to know before running it. It **writes** to `tickets.created_at` to move a
ticket's clock, which is the one thing no endpoint exposes and the only way to test a
deadline without waiting an hour; point it at a development database. It reads the
database directly for the claims no response carries — a notification's recipient, and
the organization a ticket belongs to — so it needs the same `.env` the server uses. And it
registers organizations it does not delete.

**It registers three tenants, and §45 limits registration to five an hour per address**
(`RATE_LIMIT_REGISTER_PER_HOUR`) — so the second run inside an hour is the last one that
works, and the third reports `429` from `Tenant.__init__` rather than a failed assertion.
That is the limiter behaving correctly, not the phase misbehaving, but it reads as a
traceback. Clear the bucket rather than waiting it out:

    docker compose exec redis redis-cli -n 0 --scan --pattern 'ratelimit:register:*'

**The sweep runs on a different clock from beat's.** Beat fires every
`SLA_SWEEP_INTERVAL_SECONDS`, and this script does not wait for it — it calls the task
directly, which is the same code path with the interval removed. Beat being alive is
proved separately, by the worker's log.
"""

# ruff: noqa: T201

import asyncio
import sys
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import engine
from app.core.event_loop import run
from app.workers import sla_tasks

BASE = "http://localhost:8000/api/v1"
SLA = f"{BASE}/sla"
TICKETS = f"{BASE}/tickets"
USERS = f"{BASE}/users"
AUDIT = f"{BASE}/audit-logs"
PASSWORD = "correct-horse-battery-staple"

# Mailpit's own HTTP API, which is why it is the local mail server of choice here: the
# message can be read back by the same script that caused it, rather than by a person
# looking at a UI.
MAILPIT = "http://localhost:8025"

# Long enough for a solo worker to pick up a task, open an SMTP connection, and have
# Mailpit index the result. The poll below is what makes this reliable rather than slow.
EMAIL_TIMEOUT_SECONDS = 25.0

# §27's URGENT row, as seeded: 30 minutes to a first response, 4 hours to a resolution,
# and the table's default 80% threshold. Written out rather than imported from
# `sla_service.DEFAULT_POLICIES`, because a walkthrough that derives its expectation from
# the code under test shows only that the code is self-consistent — and these are a
# transcription of a specification table.
URGENT_RESPONSE_MINUTES = 30
URGENT_RESOLUTION_MINUTES = 4 * 60
URGENT_WARNING_MINUTES = 24  # 80% of 30

# The two backdates. 25 minutes is inside the warning band and short of the deadline; 15
# more puts the ticket 40 minutes old, which is past it. Both are relative to a ticket
# whose clock started a moment ago, so "minutes ago" and "minutes elapsed" are the same
# number here — which is the one place in this script where that is true.
INTO_THE_WARNING_BAND = 25
PAST_THE_DEADLINE = 40

# How long to wait before asserting that nothing arrived. A negative cannot be polled for:
# the assertion is that Mailpit's total does not move, and the only honest version of that
# is to give the worker time to have moved it if it were going to.
QUIET_SECONDS = 3.0

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. The script runs to the end even after a failure.

    A walkthrough that stops at the first problem hides the rest of the story, and seeing
    the rest of the story is why one walks through rather than running the suite, which
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


class Api:
    """One signed-in caller: an HTTP client carrying a bearer token."""

    def __init__(self, token: str, email: str) -> None:
        self.token = token
        self.email = email
        self._client = httpx.Client(base_url=BASE, timeout=30.0)
        self._user_id: str | None = None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.request(
            method, path, headers={"Authorization": f"Bearer {self.token}"}, **kwargs
        )

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    @property
    def user_id(self) -> str:
        """This caller's own id, from `/auth/me`.

        Asked for rather than passed in: `TokenResponse` carries the access token and
        nothing else — its docstring explains why the refresh token is not there — so a
        session's identity is only knowable by asking. Reading it from the endpoint a
        client would use is what keeps the recipient assertions below about the API rather
        than about this script.
        """
        if self._user_id is None:
            response = self.get("/auth/me")
            response.raise_for_status()
            self._user_id = str(response.json()["id"])
        return self._user_id

    # --- the two collections this phase is about -------------------------------

    def policies(self) -> list[dict[str, Any]]:
        response = self.get("/sla/policies")
        response.raise_for_status()
        return list(response.json())

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        response = self.get(f"/tickets/{ticket_id}")
        response.raise_for_status()
        return dict(response.json())

    def timeline(self, ticket_id: str) -> list[dict[str, Any]]:
        response = self.get(f"/tickets/{ticket_id}/events")
        response.raise_for_status()
        return list(response.json())

    def notifications(self, **params: Any) -> list[dict[str, Any]]:
        response = self.get("/notifications", params=params)
        response.raise_for_status()
        return list(response.json())


class Tenant:
    """A registered organization, with its founding admin's session."""

    def __init__(self, label: str) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.name = f"{label} {suffix}"
        self.email = f"admin-{suffix}@walkthrough.example"
        self._counter = 0

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
        self._counter += 1
        email = f"member{self._counter}-{uuid.uuid4().hex[:6]}@walkthrough.example"

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

    def customer(self, name: str, email: str) -> dict[str, Any]:
        response = self.admin.post("/customers", json={"name": name, "email": email})
        response.raise_for_status()
        return dict(response.json())

    def ticket_for(
        self, customer_id: str, subject: str, *, priority: str = "urgent", **extra: Any
    ) -> dict[str, Any]:
        response = self.admin.post(
            "/tickets",
            json={
                "subject": subject,
                "description": "It does not work.",
                "customer_id": customer_id,
                "priority": priority,
                **extra,
            },
        )
        response.raise_for_status()
        return dict(response.json())

    def assign(self, ticket_id: str, agent_id: str) -> httpx.Response:
        return self.admin.post(f"/tickets/{ticket_id}/assign", json={"assigned_agent_id": agent_id})

    def move(self, ticket_id: str, status: str) -> httpx.Response:
        return self.admin.post(f"/tickets/{ticket_id}/status", json={"status": status})


# ---------------------------------------------------------------------------
# Mailpit
# ---------------------------------------------------------------------------


async def email_to(address: str, *, subject_contains: str = "") -> dict[str, Any] | None:
    """Poll Mailpit until a message for `address` arrives. `None` if it never does.

    Polled rather than slept on, because the delivery is asynchronous by design — the
    sweep returned before the worker had even seen the task — and a fixed sleep would be
    either flaky or slow depending on the machine.
    """
    deadline = time.monotonic() + EMAIL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        # Off the loop. This script is async only because it reads the database, and
        # `httpx` has no async client here — so the blocking call goes to a thread rather
        # than stalling the loop the database is being read on.
        for message in await _mailbox():
            recipients = {recipient["Address"] for recipient in message.get("To", [])}
            if address in recipients and subject_contains in message["Subject"]:
                return message
        await asyncio.sleep(0.5)
    return None


async def mailbox_total() -> int:
    """How many messages Mailpit has ever accepted. The negative assertion's witness."""
    response = await asyncio.to_thread(httpx.get, f"{MAILPIT}/api/v1/messages", timeout=30.0)
    response.raise_for_status()
    return int(response.json()["total"])


async def settled_mailbox_total() -> int:
    """The total, once deliveries have stopped arriving.

    **The baseline a "nothing new arrived" assertion is taken against has to be taken after
    the previous section's mail has landed, and the first draft of this script did not do
    that.** It read the total, swept, slept three seconds, and compared — and the two
    messages that arrived during that sleep were the *previous* section's manager alerts
    still in flight, not duplicates. The check failed while the behaviour it was testing was
    correct, which is the worst kind of red: it points at the product.

    So wait for two consecutive readings to agree before believing either. A quiet second is
    longer than the gap between a sweep's commit and the worker's SMTP call, and the timeout
    keeps a genuinely stuck worker from hanging the script.
    """
    deadline = time.monotonic() + EMAIL_TIMEOUT_SECONDS
    previous = await mailbox_total()
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        current = await mailbox_total()
        if current == previous:
            return current
        previous = current
    return previous


async def _mailbox() -> list[dict[str, Any]]:
    response = await asyncio.to_thread(httpx.get, f"{MAILPIT}/api/v1/messages", timeout=30.0)
    response.raise_for_status()
    return list(response.json()["messages"])


def email_text(message_id: str) -> str:
    """The plain-text body of a delivered message."""
    response = httpx.get(f"{MAILPIT}/api/v1/message/{message_id}", timeout=30.0)
    response.raise_for_status()
    return str(response.json()["Text"])


# ---------------------------------------------------------------------------
# The database, for the three things no response exposes
# ---------------------------------------------------------------------------


async def age(ticket_id: str, *, minutes: int) -> None:
    """Move a ticket's clock backwards. **The only write in this script.**

    `created_at` is both of a timer's start and the input to `due_at`, and no endpoint
    exposes it — deliberately, since a client that could set it could rewrite its own
    compliance record. So the deadline is tested by moving the clock rather than by
    waiting an hour, which is the same technique the integration suite uses.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE tickets SET created_at = created_at - make_interval(mins => :minutes) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": ticket_id, "minutes": minutes},
        )


async def organization_of(ticket_id: str) -> str:
    """The organization a ticket belongs to — what the sweep is handed.

    Read off the row rather than remembered from registration, because the id `register`
    returns is the token's, not the organization's: the caller is never told which tenant
    they are in, which is the property `TenantContext` exists to guarantee (§4 — never
    trust `organization_id` from the frontend).
    """
    async with engine.connect() as connection:
        row = await connection.execute(
            text("SELECT organization_id FROM tickets WHERE id = CAST(:id AS uuid)"),
            {"id": ticket_id},
        )
        return str(row.scalar_one())


async def recipients(ticket_id: str, *, notification_type: str) -> list[str]:
    """Who a ticket's alerts went to, by user id. Read past the API.

    Past the API because the API does not expose a recipient — a caller sees their own
    inbox and nothing else — and "the assignee and every manager" is a claim about rows,
    not about what one inbox renders. `CAST(... AS text)` because the column is a native
    PostgreSQL enum, which does not compare to a `varchar` parameter.
    """
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT user_id FROM notifications WHERE ticket_id = CAST(:id AS uuid) "
                "AND CAST(notification_type AS text) = :kind"
            ),
            {"id": ticket_id, "kind": notification_type},
        )
        return sorted(str(row.user_id) for row in rows)


async def alerts_on(ticket_id: str) -> list[str]:
    """The SLA entries on a ticket's timeline, read past the API and sorted.

    Sorted rather than ordered, and that is not laziness: the sweep writes both entries
    for one ticket inside a single transaction, and PostgreSQL's `now()` is the
    transaction's start time, so the two rows carry the same `created_at` and the
    timeline's `(created_at, id)` ordering falls through to a uuid comparison. Nothing
    reads that order — `index_alerts` takes the earliest entry per timer, and equal
    timestamps are equal either way — so the script asserts the set.
    """
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT event_type FROM ticket_events WHERE ticket_id = CAST(:id AS uuid) "
                "AND CAST(event_type AS text) IN ('sla_warning', 'sla_breached')"
            ),
            {"id": ticket_id},
        )
        return sorted(str(row.event_type) for row in rows)


async def sweep(organization_id: str) -> dict[str, int]:
    """Run one organization's sweep, exactly as beat does.

    **In a thread, and that is not incidental.** `check_organization_sla` reaches the
    database through `app/core/event_loop.run`, which builds its own `asyncio.Runner`.
    This script is already inside one — `run(main())`, at the bottom — and a second Runner
    in the same thread raises before a single query is issued. A thread has no running
    loop, so the Runner the task builds there is the only one it can see. The same
    constraint is why the worker runs with `--pool=solo` rather than `prefork`.
    """
    return await asyncio.to_thread(sla_tasks.check_organization_sla, organization_id)


def state_of(position: dict[str, Any] | None, timer: str) -> str:
    """One timer's state out of a ticket payload, with the absence spelled out.

    A `str` and never `None`, so a missing position fails the check that wanted the state
    rather than raising `TypeError` three frames down — the walkthrough's whole point is
    to keep going after a surprise and report it.
    """
    if position is None:
        return "no-sla-object"
    return str(position[timer]["state"])


# ---------------------------------------------------------------------------
# 1. The policy table a tenant starts with
# ---------------------------------------------------------------------------


async def a_new_tenant_starts_with_the_four_targets() -> Tenant:
    section("1. A new tenant starts with §27's four targets, and the desk can read them")
    tenant = Tenant("SLA Walkthrough")

    rows = tenant.admin.policies()
    check(
        "registration seeded all four, in priority order",
        [row["priority"] for row in rows] == ["low", "medium", "high", "urgent"],
        str([row["priority"] for row in rows]),
    )

    urgent = next(row for row in rows if row["priority"] == "urgent")
    check("urgent targets a 30-minute first response", urgent["response_time_minutes"] == 30)
    check("urgent targets a 4-hour resolution", urgent["resolution_time_minutes"] == 240)
    check("the warning threshold is the table's 80%", urgent["warning_threshold_percent"] == 80)
    check("and it is active", urgent["is_active"] is True)
    check("each row is addressable by its own id", all(row["id"] for row in rows))

    agent = tenant.member("agent")
    manager = tenant.member("manager")
    for role, caller in (("manager", manager), ("agent", agent)):
        check(f"a {role} can read the targets", len(caller.policies()) == 4)

    return tenant


async def editing_a_target_and_who_may(tenant: Tenant) -> None:
    section("2. An admin may move a target; nobody below admin may, and the trail records it")
    manager = tenant.member("manager")

    refused = manager.patch(f"{SLA}/policies/high", json={"response_time_minutes": 1})
    check("a manager's edit is refused", refused.status_code == 403, refused.text)
    check(
        "and changed nothing",
        next(row for row in tenant.admin.policies() if row["priority"] == "high")[
            "response_time_minutes"
        ]
        == 120,
    )

    edited = tenant.admin.patch(f"{SLA}/policies/high", json={"response_time_minutes": 180})
    check("the admin's edit succeeds", edited.status_code == 200, edited.text)
    check("the new target is stored", edited.json()["response_time_minutes"] == 180)
    check(
        "and the fields the request did not mention kept their values",
        edited.json()["resolution_time_minutes"] == 480
        and edited.json()["warning_threshold_percent"] == 80,
        str(edited.json()),
    )

    trail = tenant.admin.get(AUDIT, params={"action": "sla_policy_updated"}).json()
    check("the edit is in the audit trail", len(trail) == 1, str(len(trail)))
    if trail:
        before, after = trail[0]["extra_data"]["before"], trail[0]["extra_data"]["after"]
        check("the trail names the policy", trail[0]["target_type"] == "sla_policy")
        check("it records the old target", before["response_time_minutes"] == 120)
        check("and the new one", after["response_time_minutes"] == 180)

    empty = tenant.admin.patch(f"{SLA}/policies/high", json={})
    check("an edit naming no field is refused", empty.status_code == 422, empty.text)

    contradictory = tenant.admin.patch(
        f"{SLA}/policies/low", json={"response_time_minutes": 72 * 60 + 1}
    )
    check(
        "a response target past its resolution target is refused",
        contradictory.status_code == 422,
        contradictory.text,
    )
    check(
        "and the row did not move",
        next(row for row in tenant.admin.policies() if row["priority"] == "low")[
            "response_time_minutes"
        ]
        == 24 * 60,
    )

    typo = tenant.admin.patch(f"{SLA}/policies/critical", json={"response_time_minutes": 60})
    check("an unknown priority is refused", typo.status_code == 422, typo.text)


# ---------------------------------------------------------------------------
# 3. The clock on a ticket
# ---------------------------------------------------------------------------


async def a_ticket_carries_both_clocks(tenant: Tenant) -> dict[str, Any]:
    section("3. A ticket carries both clocks, measured against the policy in force")
    agent = tenant.member("agent")
    customer = tenant.customer("Dana Scully", f"scully-{uuid.uuid4().hex[:6]}@example.com")
    ticket = tenant.ticket_for(customer["id"], "The spectrometer is drifting")

    assigned = tenant.assign(ticket["id"], agent.user_id)
    check("the ticket is assigned to the agent", assigned.status_code == 200, assigned.text)

    read = tenant.admin.ticket(ticket["id"])
    position = read["sla"]
    check("the ticket carries an SLA position", position is not None)
    if position is None:
        return {"agent": agent, "customer": customer, "ticket": ticket}

    check(
        "each timer names itself",
        {position["response"]["timer"], position["resolution"]["timer"]}
        == {"response", "resolution"},
    )
    check(
        "the policy it was measured against comes with it",
        position["policy"]["priority"] == "urgent"
        and position["policy"]["response_time_minutes"] == 30,
        str(position["policy"]),
    )

    # Both deadlines are the policy's targets measured from `created_at`, computed by the
    # API rather than stored. `due_at - created_at` is therefore an equality and not an
    # approximation, which is the property a stored `sla_due_at` column could not keep
    # honest after a reprioritisation.
    created = datetime.fromisoformat(ticket["created_at"])
    check(
        "the response deadline is 30 minutes after creation",
        datetime.fromisoformat(position["response"]["due_at"]) - created
        == timedelta(minutes=URGENT_RESPONSE_MINUTES),
    )
    check(
        "the resolution deadline is 4 hours after creation",
        datetime.fromisoformat(position["resolution"]["due_at"]) - created
        == timedelta(minutes=URGENT_RESOLUTION_MINUTES),
    )

    check("a fresh ticket is on track", state_of(position, "response") == "on_track")
    check("and owes nothing yet", position["response"]["warned_at"] is None)
    check("its clock has not stopped", position["response"]["stopped_at"] is None)
    check(
        "and it has time to spare",
        0 < position["response"]["remaining_seconds"] <= URGENT_RESPONSE_MINUTES * 60,
        str(position["response"]["remaining_seconds"]),
    )

    return {"agent": agent, "customer": customer, "ticket": ticket}


# ---------------------------------------------------------------------------
# 4. The sweep, on a clock I control
# ---------------------------------------------------------------------------


async def the_sweep_warns_the_assignee_and_the_managers(
    tenant: Tenant, world: dict[str, Any]
) -> None:
    section("4. The sweep fires on a backdated clock — the first real-time alert")

    agent, ticket = world["agent"], world["ticket"]
    organization_id = await organization_of(ticket["id"])

    # A second manager, so the fan-out below is more than a pair and a hardcoded
    # expectation could not pass by accident. The tenant already holds the ones earlier
    # sections made; this one is here to be counted, not named (see the assertion).
    tenant.member("manager")

    await age(ticket["id"], minutes=INTO_THE_WARNING_BAND)
    read = tenant.admin.ticket(ticket["id"])
    check(
        "25 minutes in, the response clock reads warning",
        state_of(read["sla"], "response") == "warning",
        state_of(read["sla"], "response"),
    )
    check(
        "and the resolution clock is still on track",
        state_of(read["sla"], "resolution") == "on_track",
    )
    check("nothing has been said yet", read["sla"]["response"]["warned_at"] is None)

    # **The expected fan-out is read from the organization, not written down here.** §27's
    # "notify agents/managers" is the rule, and the rule is: the assignee, plus every
    # *active* manager. This tenant has accumulated managers across the earlier sections, so
    # a hardcoded count would assert the fixture rather than the rule — and reading the
    # roster back over HTTP is what makes this an independent check instead of a restatement
    # of the query `sla_repository.find_manager_ids` runs.
    managers = {
        row["id"]
        for row in tenant.admin.get(USERS).json()
        if row["role"] == "manager" and row["is_active"]
    }
    expected = {agent.user_id} | managers
    check(
        "the organization holds several managers, so the fan-out is not a pair",
        len(managers) > 1,
        str(len(managers)),
    )

    counts = await sweep(organization_id)
    check("the sweep found the ticket", counts["tickets"] == 1, str(counts))
    check("it recorded one warning", counts["warnings"] == 1, str(counts))
    check("and no breach", counts["breaches"] == 0, str(counts))
    check(
        "it staged one notification per recipient",
        counts["notifications"] == len(expected),
        f"{counts} vs {len(expected)} expected",
    )
    check(
        "and queued every one for delivery",
        counts["queued"] == len(expected),
        f"{counts} vs {len(expected)} expected",
    )

    check("the timeline records the warning", await alerts_on(ticket["id"]) == ["sla_warning"])

    entries = tenant.admin.timeline(ticket["id"])
    warning = next(row for row in entries if row["event_type"] == "sla_warning")
    check("the entry has no actor, because the system acted", warning["actor_user_id"] is None)
    check("it names the timer that fired", warning["extra_data"]["timer"] == "response")
    check(
        "and the deadline it fired against matches the clock's",
        datetime.fromisoformat(warning["extra_data"]["due_at"])
        == datetime.fromisoformat(tenant.admin.ticket(ticket["id"])["sla"]["response"]["due_at"]),
        str(warning["extra_data"]),
    )

    told = set(await recipients(ticket["id"], notification_type="sla_warning"))
    check(
        "the assignee and every active manager were told", told == expected, f"{told} != {expected}"
    )

    read = tenant.admin.ticket(ticket["id"])
    check(
        "the clock now reports when the warning was raised",
        read["sla"]["response"]["warned_at"] == warning["created_at"],
        f"{read['sla']['response']['warned_at']} != {warning['created_at']}",
    )

    message = await email_to(agent.email, subject_contains="SLA warning: first response is due")
    check("the alert email left the process and reached Mailpit", message is not None, agent.email)
    if message is not None:
        check(
            "its subject is bracketed with the product name",
            message["Subject"] == f"[{get_settings().PROJECT_NAME}] "
            f"{next(n['title'] for n in agent.notifications() if n['ticket_id'] == ticket['id'])}",
            message["Subject"],
        )
        body = email_text(message["ID"])
        check(
            "its body names the ticket by number and subject",
            f"#{ticket['number']} - The spectrometer is drifting" in body,
            body[:160],
        )


async def running_it_twice_tells_nobody_anything(tenant: Tenant, world: dict[str, Any]) -> None:
    section("5. A second sweep of the same ticket is silent — the at-most-four property")
    ticket = world["ticket"]
    organization_id = await organization_of(ticket["id"])

    before = await settled_mailbox_total()
    entries_before = len(tenant.admin.timeline(ticket["id"]))

    counts = await sweep(organization_id)
    # **`tickets` is not zero, and expecting it to be was the bug in the first draft of
    # this script.** That key counts the candidates the sweep *examined*, and the ticket is
    # still aged past its warning threshold, so it is still a candidate — the sweep looks at
    # it again and finds nothing due. Which is the stronger claim: a sweep that reported
    # zero tickets would have proved only that the query did not run, whereas this proves the
    # guard held on a ticket the sweep actually considered. The four keys that count
    # *sayings* are the ones that must be zero.
    check(
        "the second sweep says nothing",
        counts["warnings"] == 0
        and counts["breaches"] == 0
        and counts["notifications"] == 0
        and counts["queued"] == 0,
        str(counts),
    )
    check(
        "and it did consider the ticket rather than skipping it",
        counts["tickets"] == 1,
        str(counts),
    )
    check(
        "the timeline did not grow",
        len(tenant.admin.timeline(ticket["id"])) == entries_before,
    )
    check(
        "the warning is still the only SLA entry",
        await alerts_on(ticket["id"]) == ["sla_warning"],
    )

    # A negative assertion cannot be polled for, so the worker is given time to have
    # delivered a second email if one had been queued. The task above already reported
    # `queued: 0`, which is the direct evidence; this is the same claim made by the mail
    # server rather than by the code that would have done it.
    await asyncio.sleep(QUIET_SECONDS)
    check(
        "and no second email arrived, because none was queued",
        await mailbox_total() == before,
        f"{await mailbox_total()} != {before}",
    )


async def past_the_deadline_the_breach_lands_beside_the_warning(
    tenant: Tenant, world: dict[str, Any]
) -> None:
    section("6. Past the deadline, the breach is a second alert rather than a correction")
    ticket = world["ticket"]
    organization_id = await organization_of(ticket["id"])

    await age(ticket["id"], minutes=PAST_THE_DEADLINE - INTO_THE_WARNING_BAND)
    read = tenant.admin.ticket(ticket["id"])
    check(
        "40 minutes in, the response clock reads breached",
        state_of(read["sla"], "response") == "breached",
        state_of(read["sla"], "response"),
    )
    check(
        "and it reports minutes overdue, not time remaining",
        read["sla"]["response"]["remaining_seconds"] < 0,
        str(read["sla"]["response"]["remaining_seconds"]),
    )

    counts = await sweep(organization_id)
    check("the sweep records one breach", counts["breaches"] == 1, str(counts))
    check("and does not repeat the warning", counts["warnings"] == 0, str(counts))

    check(
        "the timeline holds both, because both were true when they were sent",
        await alerts_on(ticket["id"]) == ["sla_breached", "sla_warning"],
        str(await alerts_on(ticket["id"])),
    )

    read = tenant.admin.ticket(ticket["id"])
    check(
        "the clock reports the breach alongside the warning",
        read["sla"]["response"]["breached_at"] is not None
        and read["sla"]["response"]["warned_at"] is not None,
        str(read["sla"]["response"]),
    )
    check(
        "the resolution clock never warned, and still reads on track",
        state_of(read["sla"], "resolution") == "on_track",
    )


# ---------------------------------------------------------------------------
# 7. Stopping the clocks
# ---------------------------------------------------------------------------


async def stopping_the_clocks_does_not_erase_a_miss(tenant: Tenant, world: dict[str, Any]) -> None:
    section("7. Replying stops one clock, resolving the other, and neither erases a miss")
    agent, ticket = world["agent"], world["ticket"]

    replied = agent.post(f"/tickets/{ticket['id']}/messages", json={"body": "Looking at it."})
    check("the agent's public reply is accepted", replied.status_code == 201, replied.text)

    read = tenant.admin.ticket(ticket["id"])
    response = read["sla"]["response"]
    check("the reply stopped the response clock", response["stopped_at"] is not None)
    check(
        "it stopped late, so it still reads breached",
        response["state"] == "breached",
        response["state"],
    )
    check(
        "and it measures the miss rather than the time left",
        response["remaining_seconds"] < 0,
        str(response["remaining_seconds"]),
    )
    check(
        "the resolution clock was not touched by it",
        read["sla"]["resolution"]["stopped_at"] is None,
    )

    for status in ("in_progress", "resolved"):
        moved = tenant.move(ticket["id"], status)
        check(f"the ticket moves to {status}", moved.status_code == 200, moved.text)

    read = tenant.admin.ticket(ticket["id"])
    resolution = read["sla"]["resolution"]
    check("resolving stopped the resolution clock", resolution["stopped_at"] is not None)
    check(
        "well inside its four hours, so it reads met",
        resolution["state"] == "met",
        resolution["state"],
    )
    check(
        "with time to spare rather than overdue",
        resolution["remaining_seconds"] > 0,
        str(resolution["remaining_seconds"]),
    )
    check(
        "and the response clock still reads breached — a miss is not repaired by finishing",
        read["sla"]["response"]["state"] == "breached",
        read["sla"]["response"]["state"],
    )

    closed = tenant.admin.post(f"/tickets/{ticket['id']}/close")
    check("the ticket can be closed", closed.status_code == 200, closed.text)
    reopened = tenant.admin.post(f"/tickets/{ticket['id']}/reopen")
    check("and reopened", reopened.status_code == 200, reopened.text)

    read = tenant.admin.ticket(ticket["id"])
    check(
        "reopening cleared the resolution stop, because the work is no longer done",
        read["sla"]["resolution"]["stopped_at"] is None,
    )
    check(
        "so the resolution clock is running again",
        read["sla"]["resolution"]["state"] == "on_track",
        read["sla"]["resolution"]["state"],
    )


# ---------------------------------------------------------------------------
# 8. Who each alert reaches
# ---------------------------------------------------------------------------


async def who_each_alert_reaches() -> None:
    section("8. An assigned ticket warns the assignee too; an unassigned one does not")
    tenant = Tenant("Fanout")
    agent = tenant.member("agent")
    first_manager = tenant.member("manager")
    second_manager = tenant.member("manager")
    customer = tenant.customer("Ada Lovelace", f"ada-{uuid.uuid4().hex[:6]}@example.com")

    assigned = tenant.ticket_for(customer["id"], "Assigned and overdue")
    tenant.assign(assigned["id"], agent.user_id)
    orphan = tenant.ticket_for(customer["id"], "Nobody picked this up")
    organization_id = await organization_of(assigned["id"])
    for ticket in (assigned, orphan):
        await age(ticket["id"], minutes=INTO_THE_WARNING_BAND)

    counts = await sweep(organization_id)
    check("both tickets were swept", counts["tickets"] == 2, str(counts))
    check("each produced one warning", counts["warnings"] == 2, str(counts))

    check(
        "the assigned ticket reached the assignee and both managers",
        set(await recipients(assigned["id"], notification_type="sla_warning"))
        == {agent.user_id, first_manager.user_id, second_manager.user_id},
        str(await recipients(assigned["id"], notification_type="sla_warning")),
    )
    check(
        "the unassigned one reached the managers alone",
        set(await recipients(orphan["id"], notification_type="sla_warning"))
        == {first_manager.user_id, second_manager.user_id},
        str(await recipients(orphan["id"], notification_type="sla_warning")),
    )
    check(
        "the admin is not on either, because the queue is the manager's job",
        tenant.admin.user_id not in await recipients(orphan["id"], notification_type="sla_warning"),
    )

    deactivated = tenant.admin.post(f"{USERS}/{second_manager.user_id}/deactivate")
    check("a manager can be deactivated", deactivated.status_code == 200, deactivated.text)
    third = tenant.ticket_for(customer["id"], "Another one nobody picked up")
    await age(third["id"], minutes=INTO_THE_WARNING_BAND)
    await sweep(organization_id)
    check(
        "an alert to somebody who cannot sign in is not written at all",
        set(await recipients(third["id"], notification_type="sla_warning"))
        == {first_manager.user_id},
        str(await recipients(third["id"], notification_type="sla_warning")),
    )


# ---------------------------------------------------------------------------
# 9. What the portal sees
# ---------------------------------------------------------------------------


async def the_portal_sees_neither_the_clock_nor_the_deadline(
    tenant: Tenant, world: dict[str, Any]
) -> None:
    section("9. The portal gets no clock and no timeline entry — §3 withholds SLA_VIEW")
    ticket, customer = world["ticket"], world["customer"]
    portal = tenant.member("customer", customer_id=customer["id"])

    refused = portal.get(f"{SLA}/policies")
    check("a portal caller cannot read the targets", refused.status_code == 403, refused.text)

    read = portal.ticket(ticket["id"])
    check("it can still read its own ticket", read["id"] == ticket["id"])
    check("but the SLA object is null", read["sla"] is None, str(read["sla"]))

    seen = {row["event_type"] for row in portal.timeline(ticket["id"])}
    check(
        "and the timeline hides the deadline it was measured against",
        not seen & {"sla_warning", "sla_breached"},
        str(sorted(seen)),
    )
    check(
        "while staff still see them",
        {"sla_warning", "sla_breached"}
        <= {row["event_type"] for row in tenant.admin.timeline(ticket["id"])},
    )


# ---------------------------------------------------------------------------
# 10. Another tenant
# ---------------------------------------------------------------------------


async def another_tenants_policies_and_tickets_are_its_own() -> None:
    section("10. Another tenant's targets are its own, and its ticket is a 404")
    mine, theirs = Tenant("Northwind"), Tenant("Southwind")
    customer = mine.customer("Grace Hopper", f"grace-{uuid.uuid4().hex[:6]}@example.com")
    ticket = mine.ticket_for(customer["id"], "Northwind's private outage")

    mine_id = next(row["id"] for row in mine.admin.policies() if row["priority"] == "urgent")
    theirs_id = next(row["id"] for row in theirs.admin.policies() if row["priority"] == "urgent")
    check("two tenants, two rows for the same priority", mine_id != theirs_id)

    edited = theirs.admin.patch(f"{SLA}/policies/urgent", json={"response_time_minutes": 5})
    check("a second tenant's edit succeeds", edited.status_code == 200, edited.text)
    check(
        "and did not move the first tenant's target",
        next(row for row in mine.admin.policies() if row["priority"] == "urgent")[
            "response_time_minutes"
        ]
        == URGENT_RESPONSE_MINUTES,
    )

    refused = theirs.admin.get(f"{TICKETS}/{ticket['id']}")
    absent = theirs.admin.get(f"{TICKETS}/{uuid.uuid4()}")
    check("a ticket across the boundary is a 404", refused.status_code == 404, refused.text)
    check(
        "indistinguishable from an id nobody wrote",
        refused.json() == absent.json(),
        f"{refused.json()} != {absent.json()}",
    )
    check("and it leaks no subject line", "Northwind's private outage" not in refused.text)

    # A swept tenant with nothing due writes nothing anywhere — the strongest form, since
    # it is a statement about rows rather than about a refusal.
    counts = await sweep(await organization_of(ticket["id"]))
    check(
        "a sweep of a tenant with no overdue ticket changes nothing",
        counts == {"tickets": 0, "warnings": 0, "breaches": 0, "notifications": 0, "queued": 0},
        str(counts),
    )
    check("so its ticket has no alert", await alerts_on(ticket["id"]) == [])

    # An id nothing matches is not an error, and it writes nothing. The dispatcher only
    # ever hands over ids it read from the organizations table, so this is the defensive
    # direction: a filter that fell back to "all organizations" would be the failure mode
    # of an accidentally optional `WHERE`, and it would look like a successful sweep.
    unknown = await sweep(str(uuid.uuid4()))
    check(
        "and an unknown organization id sweeps nothing rather than everything",
        unknown == {"tickets": 0, "warnings": 0, "breaches": 0, "notifications": 0, "queued": 0},
        str(unknown),
    )
    check("leaving the first tenant's ticket untouched", await alerts_on(ticket["id"]) == [])


async def main() -> None:
    # Section numbers run in the order they print. The portal check is 9 and the fan-out is
    # 8, which were called the other way round in the first draft — the two are independent
    # of each other, and a walkthrough that prints "9" before "8" costs the reader a moment
    # for no reason.
    tenant = await a_new_tenant_starts_with_the_four_targets()
    await editing_a_target_and_who_may(tenant)
    world = await a_ticket_carries_both_clocks(tenant)
    if "agent" in world:
        await the_sweep_warns_the_assignee_and_the_managers(tenant, world)
        await running_it_twice_tells_nobody_anything(tenant, world)
        await past_the_deadline_the_breach_lands_beside_the_warning(tenant, world)
        await stopping_the_clocks_does_not_erase_a_miss(tenant, world)
        await who_each_alert_reaches()
        await the_portal_sees_neither_the_clock_nor_the_deadline(tenant, world)
    else:
        check("an SLA position was returned for the ticket", False, "it was null")

    await another_tenants_policies_and_tickets_are_its_own()

    print(f"\n{_passed} passed, {_failed} failed")
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        run(main())
    except httpx.ConnectError as exc:
        print(
            f"\nCould not reach {exc.request.url}. This walkthrough needs all three:\n"
            "  API:    .venv/Scripts/python.exe -m uvicorn app.main:app "
            "--loop app.core.event_loop:loop_factory --port 8000\n"
            "  worker: .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker "
            "--loglevel=info --pool=solo -Q notifications,sla\n"
            "  beat:   .venv/Scripts/python.exe -m celery -A app.workers.celery_app beat "
            "--loglevel=info -s /tmp/celerybeat-schedule",
            file=sys.stderr,
        )
        sys.exit(2)
