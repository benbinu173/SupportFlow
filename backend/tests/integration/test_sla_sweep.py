"""The SLA sweep, run for real: what it records, who it tells, and what it refuses to repeat.

**This is Phase Q's central claim, and it is the only file that can make it.** Everything
upstream is a pure function or a single query: `tests/unit/test_sla_clock.py` proves the
clock exhaustively against constructed tickets, and `tests/api/test_ticket_sla.py` proves the
API renders it. Neither can prove that a task nobody requested, running on a loop of its own,
finds the right tickets in the right tenant, writes the alert to the timeline, addresses it
to the right people, and then — this is the one worth the file — **does nothing at all the
second time it runs**.

**The sweep is invoked as a function, not awaited.** `check_organization_sla` is a Celery
task whose body is `event_loop.run(_sweep(...))`, so calling it from a synchronous test runs
the identical code path a worker would, on a loop the task owns (ADR-011). Awaiting `_sweep`
directly would run it inside `pytest-asyncio`'s loop instead, which is a different loop and
not the one a worker uses. `tests/unit/test_email_delivery.py` makes the same argument for
its own task.

**Time is moved by moving the ticket.** A backdated `created_at` is a ticket that has been
open that long as far as every reader is concerned, which is the whole consequence of the
position being derived rather than stored. That also means this file needs no clock
substitution, no sleeping, and no tolerance: the assertions are exact.

**What is asserted about the emails is deliberately weak, and the reason is the topology.**
`enqueue_delivery` publishes to a broker, and the suite replaces `.delay` with a recorder
(`tests/conftest.py`'s `queued_emails`) because no test should reach Redis. So what is
provable here is that the right *number* of deliveries were handed over, in the same order
the rows were staged. That each id resolves to a readable row and a real message is
`tests/unit/test_email_delivery.py`'s subject and `scripts/phase_q_walkthrough.py`'s.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from app.workers import sla_tasks
from tests.conftest import NOTIFICATIONS, TICKETS, USERS, OrgSession

pytestmark = pytest.mark.integration

# `URGENT`'s seeded targets, §27's table: 30 minutes to a first response and 240 to a
# resolution, warning at 80%. Chosen for this file because it is the only priority whose
# whole clock fits inside a single test run — 24 minutes to the response warning and 30 to
# the breach, so a backdate of 25 or 40 minutes moves one timer and leaves the other alone.
RESPONSE_MINUTES = 30
RESPONSE_WARNING_MINUTES = 24

# How far the standard backdate moves a ticket, and the two alert types this file is about.
# The `sla_` prefix is also what `SLA_NOTIFICATION_TYPES` filters on below.
BACKDATE_MINUTES = 25
SLA_NOTIFICATION_TYPES = ("sla_warning", "sla_breached")


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file.

    **This file is the first in `tests/integration/` that touches the database**, and
    `truncate_tables` is not autouse in the root conftest — requesting it pulls in a live
    Postgres, which is the property that keeps `tests/unit/` runnable with nothing started.
    The package cannot opt in as a whole the way `tests/api/` does, because
    `test_celery_wiring.py` deliberately contacts no service and says so: asserting on the
    Celery app's configuration should not require a database. So the opt-in is per file,
    which is also what `tests/security/` does for the four files there that need it.

    Without this the suite's organizations accumulate and `register_org`'s per-fixture
    counter hands two tests the same admin email — a collision that surfaces as an
    unrelated assertion failing in whichever test ran second.
    """
    # Nothing to do in the body: `truncate_tables` yields, so depending on it is what
    # places the truncation on the far side of the test.


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Sweep Co")


@pytest.fixture
def staff(org: OrgSession) -> dict[str, OrgSession]:
    """The three roles the fan-out distinguishes: admin, two managers, one agent.

    Two managers because one cannot distinguish "every manager" from "a manager". The
    second is deactivated in the one test that asks whether an unreachable recipient is
    skipped.
    """
    return {
        "admin": org,
        "manager": org.add_user("manager", email="manager@sweepco.com"),
        "manager2": org.add_user("manager", email="manager2@sweepco.com"),
        "agent": org.add_user("agent", email="agent@sweepco.com"),
    }


@pytest.fixture
def customer(org: OrgSession) -> str:
    return str(org.add_customer(name="Alan Turing", email="alan@bletchley.uk")["id"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def urgent_ticket(staff: dict[str, OrgSession], customer: str, **kwargs: object) -> dict:
    return staff["admin"].add_ticket(customer, priority="urgent", **kwargs)


def backdate(engine: Engine, ticket_id: object, *, minutes: int) -> None:
    """Age a ticket's clock, which is the only clock this file moves."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tickets SET created_at = created_at - make_interval(mins => :minutes) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(ticket_id), "minutes": minutes},
        )


def stored_created_at(engine: Engine, ticket_id: object) -> datetime:
    """The ticket's `created_at` as the database now holds it.

    Needed because the tests that age a ticket do so *after* the API returned it, so the
    `created_at` in the creation response is the value from before the backdate. Reading it
    back is what lets the deadline assertion be an equality against the stored row rather
    than against arithmetic the test did to itself.
    """
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT created_at FROM tickets WHERE id = CAST(:id AS uuid)"),
            {"id": str(ticket_id)},
        ).scalar_one()
    return cast("datetime", value)


def organization_of(engine: Engine, ticket_id: object) -> str:
    """The tenant a ticket belongs to, read off the row rather than assumed.

    A ticket's `organization_id` is not in `TicketRead` — like every other read model here,
    the response omits it because the token already says which tenant the caller is in. The
    sweep takes one as its argument, so the test has to get it from somewhere, and the row
    the API just created is the honest source.
    """
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT organization_id FROM tickets WHERE id = CAST(:id AS uuid)"),
                {"id": str(ticket_id)},
            ).scalar_one()
        )


def sweep(engine: Engine, ticket_id: object) -> dict[str, int]:
    """Run one organization's sweep, the way a worker runs it.

    The task, not `_sweep`: the task is what owns the event loop and what beat calls, so
    this exercises the real entry point including its `uuid.UUID` conversion. The
    dispatcher above it (`check_sla_deadlines`) is about fan-out across tenants and is
    asserted separately.
    """
    return sla_tasks.check_organization_sla(organization_of(engine, ticket_id))


def timeline(session: OrgSession, ticket_id: object) -> list[dict]:
    response = session.get(f"{TICKETS}/{ticket_id}/events")
    assert response.status_code == 200, response.text
    return [row for row in response.json() if row["event_type"] in ("sla_warning", "sla_breached")]


def staged(engine: Engine, ticket_id: object) -> list[tuple[str, str]]:
    """Every SLA notification row for a ticket, as `(user_id, type)`, sorted.

    Read from the table rather than from the API because the recipients are *different
    users* — an assignee's inbox and a manager's inbox are two different authenticated
    sessions, and asking both would be three requests to learn one fact. The rows are the
    fact. Nothing here is hidden from any of the callers involved; this is about concision
    rather than about reaching past an authorization boundary.

    **Filtered to the two SLA types, because assigning a ticket notifies too.** An
    `ASSIGNED` ticket stages a `TICKET_ASSIGNED` row for its new agent, so an unfiltered
    count would be "who heard anything about this ticket", which is a different question
    from the one Phase Q is answering — and one that would make the fan-out assertions
    below pass or fail for reasons that have nothing to do with the sweep.

    Sorted in Python rather than by the query, so the order the assertions compare against
    is the order they built their expected value in. `ORDER BY user_id` on a `uuid` column
    would happen to agree — the canonical text form is fixed-width hex — but "two
    orderings that agree today" is the shape of a test that breaks for a reason nobody can
    read.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT user_id, notification_type FROM notifications "
                "WHERE ticket_id = CAST(:id AS uuid) "
                "AND CAST(notification_type AS text) IN :types"
            ).bindparams(bindparam("types", value=SLA_NOTIFICATION_TYPES, expanding=True)),
            {"id": str(ticket_id)},
        ).all()
    return sorted((str(row.user_id), str(row.notification_type)) for row in rows)


def inbox(session: OrgSession, **params: object) -> list[dict]:
    response = session.get(NOTIFICATIONS, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The warning
# ---------------------------------------------------------------------------


def test_a_ticket_inside_its_warning_band_is_warned_about_once(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
    queued_emails: list[str],
) -> None:
    """25 minutes into a 30-minute target: the response warning fires, and only it.

    The resolution timer is asserted in the same test as the control — 25 minutes is not
    near its own warning at 192 — so this is one alert and not two, which is what makes
    "each timer is judged separately" observable rather than asserted.
    """
    ticket = urgent_ticket(staff, customer, subject="Everything is on fire")
    backdate(sync_engine, ticket["id"], minutes=25)

    counts = sweep(sync_engine, ticket["id"])

    assert counts["warnings"] == 1
    assert counts["breaches"] == 0
    entries = timeline(staff["admin"], ticket["id"])
    assert [row["event_type"] for row in entries] == ["sla_warning"]
    assert entries[0]["extra_data"]["timer"] == "response"
    assert entries[0]["actor_user_id"] is None
    assert len(queued_emails) == counts["queued"] == 2


def test_the_timeline_entry_records_the_deadline_it_fired_against(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
) -> None:
    """`extra_data.due_at` is the deadline as it stood when the alert was sent.

    Asserted against the clock's own answer rather than against a literal, because the
    literal depends on when the test ran. What matters is that the value is *stored*: a
    policy edited next month changes what the API computes for this ticket and must not
    change what the timeline says happened.
    """
    ticket = urgent_ticket(staff, customer)
    backdate(sync_engine, ticket["id"], minutes=BACKDATE_MINUTES)

    sweep(sync_engine, ticket["id"])

    (entry,) = timeline(staff["admin"], ticket["id"])
    due_at = datetime.fromisoformat(entry["extra_data"]["due_at"])
    assert due_at == stored_created_at(sync_engine, ticket["id"]) + timedelta(
        minutes=RESPONSE_MINUTES
    )


def test_a_warning_reaches_the_assignee_and_every_manager(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
) -> None:
    """§27's "notify agents/managers", which is a fan-out it does not specify.

    Settled as both: the assignee because they are the one who can act, and the managers
    because a deadline is the queue owner's problem whether or not somebody picked the
    ticket up. The admin is asserted *absent* — §3's matrix describes the queue as the
    manager's job, and an alert set that included everyone who could act would be
    indistinguishable from the notification centre.
    """
    ticket = urgent_ticket(staff, customer)
    assigned = staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )
    assert assigned.status_code == 200, assigned.text
    backdate(sync_engine, ticket["id"], minutes=25)

    sweep(sync_engine, ticket["id"])

    assert staged(sync_engine, ticket["id"]) == sorted(
        [
            (staff["agent"].user_id, "sla_warning"),
            (staff["manager"].user_id, "sla_warning"),
            (staff["manager2"].user_id, "sla_warning"),
        ]
    )


def test_a_deactivated_manager_is_not_told(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
) -> None:
    """An alert addressed to an account that cannot sign in is an alert nobody reads.

    The delivery task refuses to email it, nobody marks it read, and the badge never
    clears — so the row is not written at all rather than written and stranded. The same
    rule `notification_service._portal_users_for` applies to portal logins, applied to the
    other end of the organization.
    """
    ticket = urgent_ticket(staff, customer)
    backdate(sync_engine, ticket["id"], minutes=25)
    deactivated = staff["admin"].post(f"{USERS}/{staff['manager2'].user_id}/deactivate")
    assert deactivated.status_code == 200, deactivated.text

    sweep(sync_engine, ticket["id"])

    recipients = {user_id for user_id, _ in staged(sync_engine, ticket["id"])}
    assert recipients == {staff["manager"].user_id}


def test_an_unassigned_ticket_reaches_the_managers_alone(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
) -> None:
    """Nobody owns it, so the queue's owner is told — and never *nobody*.

    A ticket with no assignee and no manager would be an SLA alert with no recipient, which
    is the exact failure the feature exists to prevent: the target is missed, the ticket
    reads breached, and no one was ever told. That is why the fan-out includes a role that
    the ticket does not have to name.
    """
    ticket = urgent_ticket(staff, customer)
    backdate(sync_engine, ticket["id"], minutes=25)

    sweep(sync_engine, ticket["id"])

    recipients = {user_id for user_id, _ in staged(sync_engine, ticket["id"])}
    assert recipients == {staff["manager"].user_id, staff["manager2"].user_id}


def test_the_assignee_can_read_the_alert_from_their_own_inbox(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
) -> None:
    """The rows above are addressed correctly *and* reachable by the person addressed.

    A notification with the right `user_id` and a broken read path is invisible, and every
    assertion in this file except this one would still pass. The portal route is the one
    the assignee actually uses, so it is the one asserted.
    """
    ticket = urgent_ticket(staff, customer)
    staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )
    backdate(sync_engine, ticket["id"], minutes=25)

    sweep(sync_engine, ticket["id"])

    unread = inbox(staff["agent"], unread_only=True)
    kinds = sorted(row["notification_type"] for row in unread)
    # Two, and the assignment is the other one: assigning a ticket to an agent is §26's
    # "ticket assigned" and this ticket was assigned on the way to being warned about. Both
    # are in the same inbox because they are both news about the same ticket — which is
    # also why `staged` above filters to the SLA types rather than counting the lot.
    assert kinds == ["sla_warning", "ticket_assigned"]
    warning = next(row for row in unread if row["notification_type"] == "sla_warning")
    assert warning["ticket_id"] == ticket["id"]
    # The body carries the ticket's number and subject. The number and not the title,
    # because the title names the deadline rather than the ticket — an SLA alert is read in
    # a list of alerts, where "#1042" is what identifies which ticket it is about.
    assert str(ticket["number"]) in warning["body"]
    # The manager's inbox holds the same alert and no assignment notice, and the admin's
    # holds nothing at all — the read path is per-user, so this is what proves the rows were
    # addressed rather than broadcast. The admin's emptiness is the self-notification rule:
    # they were the one who assigned the ticket.
    assert [row["notification_type"] for row in inbox(staff["manager"])] == ["sla_warning"]
    assert inbox(staff["admin"]) == []


# ---------------------------------------------------------------------------
# The breach
# ---------------------------------------------------------------------------


def test_past_the_deadline_the_breach_replaces_the_warning(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
) -> None:
    """40 minutes into a 30-minute target, the first thing said is the breach.

    Not the warning and then the breach. The state is monotone and `now` only advances, so
    a timer already past its deadline the first time the sweep looks at it sends the
    breach alone — "you have six minutes" arriving after the deadline would be worse than
    the miss it describes. Asserted from a ticket the sweep has never seen, which is the
    only way to observe the precedence rather than the guard.
    """
    ticket = urgent_ticket(staff, customer)
    backdate(sync_engine, ticket["id"], minutes=40)

    counts = sweep(sync_engine, ticket["id"])

    assert counts["breaches"] == 1
    assert counts["warnings"] == 0
    assert [row["event_type"] for row in timeline(staff["admin"], ticket["id"])] == ["sla_breached"]


def test_a_warned_ticket_that_breaches_is_told_a_second_time(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
) -> None:
    """The two alerts are independent, and this walks a ticket through both.

    First sweep at 25 minutes: the warning. Then 20 more minutes of the ticket's life, and
    a second sweep: the breach. Two entries, two notification sets, no duplication —
    which is the property that makes the failure worth reporting twice, since "there is
    still time" and "there is no time" are different facts and both were true when sent.
    """
    ticket = urgent_ticket(staff, customer)
    backdate(sync_engine, ticket["id"], minutes=25)
    sweep(sync_engine, ticket["id"])
    backdate(sync_engine, ticket["id"], minutes=20)

    counts = sweep(sync_engine, ticket["id"])

    assert counts["breaches"] == 1
    assert counts["warnings"] == 0
    assert [row["event_type"] for row in timeline(staff["admin"], ticket["id"])] == [
        "sla_warning",
        "sla_breached",
    ]
    assert {kind for _, kind in staged(sync_engine, ticket["id"])} == {
        "sla_warning",
        "sla_breached",
    }


# ---------------------------------------------------------------------------
# The property that matters: running it again says nothing
# ---------------------------------------------------------------------------


def test_a_second_sweep_of_the_same_ticket_produces_nothing(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
    queued_emails: list[str],
) -> None:
    """The idempotency guard, asserted by running the sweep twice and counting.

    This is the claim most worth breaking on purpose. The sweep runs every
    `SLA_SWEEP_INTERVAL_SECONDS` forever, so anything it says twice it says twice *every
    interval* — a duplicate notification is not a cosmetic bug, it is the failure that
    makes people stop reading them, and there is nothing downstream that would catch it.

    The guard is the `SLA_WARNING` / `SLA_BREACHED` timelines entries read back through
    `index_alerts`, so this asserts the loop closes: what the sweep wrote is what stops it
    writing. All four counts are asserted, including the two that are zero, because
    "nothing was staged" and "nothing was queued" are different failures — rows written and
    not enqueued is a silent alert, and rows enqueued twice is a loud one.

    The email count is checked against the first run's, not against zero, because the first
    run's messages are still in `queued_emails`: the recorder is per-test and this test runs
    the sweep twice inside it.
    """
    ticket = urgent_ticket(staff, customer)
    backdate(sync_engine, ticket["id"], minutes=25)
    first = sweep(sync_engine, ticket["id"])
    after_first = len(queued_emails)

    second = sweep(sync_engine, ticket["id"])

    assert first["warnings"] == 1
    assert second == {
        "tickets": 1,
        "warnings": 0,
        "breaches": 0,
        "notifications": 0,
        "queued": 0,
    }
    assert len(queued_emails) == after_first
    assert len(timeline(staff["admin"], ticket["id"])) == 1
    assert len(staged(sync_engine, ticket["id"])) == 2


def test_the_sweep_is_silent_for_a_ticket_still_inside_its_target(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
) -> None:
    """A fresh ticket is not even a candidate, and that is the index's predicate.

    `find_pending` bounds by the response warning instant, so a ticket younger than that is
    never returned — the sweep does not compute a position for something that cannot be
    due. Asserted because the alternative (candidates filtered after the fact) would look
    identical from outside while scanning every open ticket in the tenant every five
    minutes.
    """
    ticket = urgent_ticket(staff, customer)

    counts = sweep(sync_engine, ticket["id"])

    assert counts == {"tickets": 0, "warnings": 0, "breaches": 0, "notifications": 0, "queued": 0}
    assert timeline(staff["admin"], ticket["id"]) == []


def test_a_terminal_ticket_is_never_a_candidate(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
) -> None:
    """A resolved ticket stops being swept, however overdue it is.

    `find_pending` excludes the two terminal statuses, which is the predicate
    `ix_tickets_sla_pending` is partial on. The clock would still report a breach — it does
    not read status, by design — so this is the sweep's judgement rather than the clock's:
    alerting a manager about a deadline on work that is finished is noise, and §28's
    compliance metric reads finished work from the ticket, not from a notification.
    """
    ticket = urgent_ticket(staff, customer)
    backdate(sync_engine, ticket["id"], minutes=40)
    assigned = staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )
    assert assigned.status_code == 200, assigned.text
    for status in ("in_progress", "resolved"):
        moved = staff["admin"].post(f"{TICKETS}/{ticket['id']}/status", json={"status": status})
        assert moved.status_code == 200, moved.text

    counts = sweep(sync_engine, ticket["id"])

    assert counts["tickets"] == 0
    assert timeline(staff["admin"], ticket["id"]) == []


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


def test_the_dispatcher_hands_each_active_organization_its_own_task(
    staff: dict[str, OrgSession],
    customer: str,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check_sla_deadlines` fans out and returns what it dispatched, without doing the work.

    The fan-out is the isolation property: one tenant with a pathological backlog must not
    delay another's alerts, and a failure has to be attributable to a tenant. Asserted by
    recording what `.delay` was handed rather than by running anything — the alternative
    would make this test's cost proportional to the number of organizations in the database,
    which is not a fixture this suite controls.

    `check_organization_sla` is invoked as a function in every other test in this file, so
    what is checked here is only that the dispatcher addresses the right tenant. It is also
    the second seam the suite replaces, after `send_notification_email.delay`.
    """
    ticket = urgent_ticket(staff, customer)
    expected = organization_of(sync_engine, ticket["id"])
    dispatched: list[str] = []
    monkeypatch.setattr(sla_tasks.check_organization_sla, "delay", dispatched.append)

    result = sla_tasks.check_sla_deadlines()

    assert result["dispatched"] == len(dispatched)
    assert len(dispatched) == len(set(dispatched)), "a tenant must be dispatched once"
    assert expected in dispatched
    # Strings, not `uuid.UUID` objects: the broker serializes to JSON, and the task's own
    # docstring says so — which is why the argument is typed `str` and the conversion
    # happens inside the task rather than being smuggled through as an object.
    assert all(uuid.UUID(value) for value in dispatched)
