"""Notifications over HTTP: what produces one, who gets it, and how it is read.

The unit test next door (`tests/unit/test_notification_policy.py`) decides who an event
notifies, against a session that refuses to query. This file is the other half: the same
policy driven through the real API against real Postgres, so the parts a unit test has to
stand in for are exercised for real — the resolution that has to *look up* the customer's
portal logins, the transaction the notification row is staged inside, and the enqueue that
must not happen before that transaction commits.

**Three claims here that nothing else can make.**

* **A resolution reaches every portal login its customer has.** A customer record may
  have more than one, `POST /users` accepts it, and the first implementation of the
  lookup assumed otherwise — `scalar_one_or_none` over two rows is a
  `MultipleResultsFound`, which surfaced as a 500 on a resolution. The regression test
  for that is deliberately the ordinary-looking one, `test_resolving_notifies_every_portal_login`.
* **The email is queued only once the row is visible.** The task takes an id and reads
  the row, so a task queued before the commit finds nothing and silently does nothing.
  Rather than assert on call order, the test replaces the enqueue with a function that
  *looks the row up* — which is the same question the task asks, asked at the moment the
  task would be handed over.
* **The payload carries no tenant and no recipient.** §4's rules are mostly about what
  does not reach a client, and the shape of a response is where that is observable.

**Two tests do read the database directly**, and it is worth saying why: both assert that
a notification was *not* written, and an absence is not observable through an API that
only ever shows a caller their own rows. `sync_engine` is the suite's own connection to
the same test database, and a row count from it is evidence in a way that "the users I
can log in as saw nothing" is not.
"""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.workers import email_tasks
from tests.conftest import NOTIFICATIONS, TICKETS, OrgSession

pytestmark = pytest.mark.integration


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Notify Co")


@pytest.fixture
def staff(org: OrgSession) -> dict[str, OrgSession]:
    """One authenticated user per staff role, all in the same organization."""
    return {
        "admin": org,
        "manager": org.add_user("manager", email="manager@notifyco.com"),
        "agent": org.add_user("agent", email="agent@notifyco.com"),
        "agent2": org.add_user("agent", email="agent2@notifyco.com"),
    }


@pytest.fixture
def customer(org: OrgSession) -> dict[str, object]:
    """A customer record plus one portal login linked to it."""
    record = org.add_customer(name="Ada Lovelace", email="ada@analytical.com")
    return {"record": record, "session": org.add_portal_user(record["id"])}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def inbox(session: OrgSession, **params: object) -> list[dict[str, object]]:
    response = session.get(NOTIFICATIONS, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def unread(session: OrgSession) -> int:
    response = session.get(f"{NOTIFICATIONS}/unread-count")
    assert response.status_code == 200, response.text
    return int(response.json()["unread"])


def assign(session: OrgSession, ticket_id: object, agent_id: object) -> None:
    response = session.post(f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": agent_id})
    assert response.status_code == 200, response.text


def move(session: OrgSession, ticket_id: object, status: str) -> None:
    response = session.post(f"{TICKETS}/{ticket_id}/status", json={"status": status})
    assert response.status_code == 200, response.text


def stored_notifications(engine: Engine, ticket_id: object) -> int:
    """How many notification rows exist for a ticket, across every user.

    The subject of the query is a ticket the test created, and the only reason to reach
    past the API for it is that "no row was written" cannot be asked of an endpoint that
    shows each caller only their own.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM notifications WHERE ticket_id = CAST(:id AS uuid)"),
                {"id": str(ticket_id)},
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def test_assigning_a_ticket_notifies_the_new_agent(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """§26's "ticket assigned", end to end, with the content the reader sees.

    The actor's own inbox is asserted empty in the same test, because the interesting
    failure of a self-notification rule is not that it fires when it should — it is that
    it fires *always*, and the manager who assigned the ticket is the one person certain
    to be looking at the screen when it does.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"], subject="Printer on fire")

    assign(staff["manager"], ticket["id"], staff["agent"].user_id)

    mine = inbox(staff["agent"])
    assert len(mine) == 1
    assert mine[0]["notification_type"] == "ticket_assigned"
    assert mine[0]["title"] == "Ticket assigned to you"
    assert mine[0]["body"] == f"#{ticket['number']} - Printer on fire"
    assert mine[0]["ticket_id"] == ticket["id"]
    assert mine[0]["read_at"] is None

    assert inbox(staff["manager"]) == []


def test_the_payload_carries_no_tenant_and_no_recipient(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """The response is exactly this set of fields, asserted as a set and not as members.

    A membership assertion would pass with `organization_id` added. Equality is what
    makes this a check on absence: no tenant id, no recipient id, and no `emailed_at`.
    The first two describe rows the caller has no business enumerating, and the third is
    delivery bookkeeping that would invite a client to render "not yet emailed" as a
    state a user should act on.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)

    assert set(inbox(staff["agent"])[0]) == {
        "id",
        "notification_type",
        "title",
        "body",
        "ticket_id",
        "read_at",
        "created_at",
    }


def test_reassigning_names_it_a_reassignment(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """§26 lists assigned and reassigned separately, and both agents' inboxes agree.

    The first agent keeps the notification they were sent — nothing retracts it, and a
    ticket that was briefly theirs is part of their history. The second gets a different
    type and a different sentence, because work arriving for the first time and work
    handed over from a colleague are not the same news.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])

    assign(staff["manager"], ticket["id"], staff["agent"].user_id)
    assign(staff["manager"], ticket["id"], staff["agent2"].user_id)

    first = inbox(staff["agent"])
    assert [item["notification_type"] for item in first] == ["ticket_assigned"]

    second = inbox(staff["agent2"])
    assert len(second) == 1
    assert second[0]["notification_type"] == "ticket_reassigned"
    assert second[0]["title"] == "Ticket reassigned to you"


def test_assigning_a_ticket_to_yourself_notifies_nobody(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """An alert about your own click is the first entry a user learns to ignore."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])

    assign(staff["manager"], ticket["id"], staff["manager"].user_id)

    assert inbox(staff["manager"]) == []


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------


def test_a_customer_reply_notifies_the_assigned_agent(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """§26's "new customer reply". The author is a portal caller, so `sender_type` is CUSTOMER."""
    record = customer["record"]
    portal = customer["session"]
    assert isinstance(record, dict)
    assert isinstance(portal, OrgSession)
    ticket = staff["manager"].add_ticket(record["id"], subject="Printer on fire")
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)

    response = portal.post(f"{TICKETS}/{ticket['id']}/messages", json={"body": "Any news?"})
    assert response.status_code == 201, response.text

    types = [item["notification_type"] for item in inbox(staff["agent"])]
    assert types == ["new_customer_reply", "ticket_assigned"]


def test_an_agent_reply_notifies_nobody(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """The agent writing the reply is the person §26 would notify, so nothing is owed.

    Read from `message.sender_type` rather than from the caller's role, which is what
    makes this hold for a manager replying on someone else's ticket too — the staff side
    is the staff side.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)

    response = staff["agent"].post(f"{TICKETS}/{ticket['id']}/messages", json={"body": "Looking."})
    assert response.status_code == 201, response.text

    assert [item["notification_type"] for item in inbox(staff["agent"])] == ["ticket_assigned"]


def test_an_internal_note_notifies_nobody(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """A note is for the desk. §26 does not list it, and the customer must not see it at all."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)

    response = staff["agent"].post(
        f"{TICKETS}/{ticket['id']}/notes", json={"body": "Escalating internally."}
    )
    assert response.status_code == 201, response.text

    assert [item["notification_type"] for item in inbox(staff["agent"])] == ["ticket_assigned"]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolving_notifies_the_customers_portal_login(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """§26's "ticket resolved", addressed to the external party rather than to staff."""
    record = customer["record"]
    portal = customer["session"]
    assert isinstance(record, dict)
    assert isinstance(portal, OrgSession)
    ticket = staff["manager"].add_ticket(record["id"], subject="Printer on fire")
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)
    move(staff["agent"], ticket["id"], "in_progress")

    move(staff["agent"], ticket["id"], "resolved")

    theirs = inbox(portal)
    assert len(theirs) == 1
    assert theirs[0]["notification_type"] == "ticket_resolved"
    assert theirs[0]["title"] == "Your ticket has been resolved"


def test_resolving_notifies_every_portal_login_the_customer_has(
    staff: dict[str, OrgSession], org: OrgSession
) -> None:
    """**The regression test for a 500.** Two logins on one customer record.

    `POST /users` permits a second portal account for the same customer — two contacts at
    one company is an ordinary thing for a support desk — and the first version of the
    lookup resolved a customer to *a* user with `scalar_one_or_none`. Two rows made that
    raise `MultipleResultsFound` inside the resolution's transaction, so a request that
    should have notified the customer instead returned a 500 and told nobody.

    Notifying all of them is the fix rather than picking one, because picking one is
    choosing which of the customer's addresses goes without the news.
    """
    record = org.add_customer(name="Analytical Engines Ltd", email="engines@notifyco.com")
    first = org.add_portal_user(record["id"], email="first@analytical.com")
    second = org.add_portal_user(record["id"], email="second@analytical.com")

    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)
    move(staff["agent"], ticket["id"], "in_progress")
    move(staff["agent"], ticket["id"], "resolved")

    for portal in (first, second):
        theirs = inbox(portal)
        assert len(theirs) == 1
        assert theirs[0]["notification_type"] == "ticket_resolved"


def test_resolving_a_ticket_whose_customer_has_no_portal_login_notifies_nobody(
    staff: dict[str, OrgSession], org: OrgSession, sync_engine: Engine
) -> None:
    """No portal account means no recipient, and `notifications.user_id` is `NOT NULL`.

    A known limitation rather than a solved problem — the record exists and nobody can
    sign in as it, so the resolution is recorded on the timeline and announced to no one.
    See ADR-023 and the README.

    The row count is read directly because the claim is an absence, and every endpoint
    here shows a caller only their own rows — so "the two staff accounts I can log in as
    saw nothing" would be consistent with a notification written to a user I cannot log
    in as, which is precisely the mistake worth catching.
    """
    record = org.add_customer(name="No Login Ltd", email="nologin@notifyco.com")
    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)
    move(staff["agent"], ticket["id"], "in_progress")

    move(staff["agent"], ticket["id"], "resolved")

    # The agent's one notification is the assignment; the resolution produced nothing.
    assert [item["notification_type"] for item in inbox(staff["agent"])] == ["ticket_assigned"]
    assert stored_notifications(sync_engine, ticket["id"]) == 1


def test_a_deactivated_portal_login_is_not_notified(
    staff: dict[str, OrgSession], org: OrgSession, sync_engine: Engine
) -> None:
    """An account the organization has cut off is treated as absent, not merely unread.

    It is the same absence as having no login at all: the person cannot sign in to see
    the notification and would still be mailed about a ticket they no longer work on.
    """
    record = org.add_customer(name="Former Customer", email="former@notifyco.com")
    portal = org.add_portal_user(record["id"])
    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)
    move(staff["agent"], ticket["id"], "in_progress")

    deactivated = staff["admin"].post(f"/api/v1/users/{portal.user_id}/deactivate")
    assert deactivated.status_code == 200, deactivated.text

    move(staff["agent"], ticket["id"], "resolved")

    # One: the assignment.
    assert stored_notifications(sync_engine, ticket["id"]) == 1


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_the_inbox_is_newest_first_and_pages(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """Three assignments, then a page of two and the last one.

    Newest first is a requirement, not a preference: the thing a user opens a
    notification list for is whatever just happened.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    created = [staff["manager"].add_ticket(record["id"], subject=f"Ticket {n}") for n in range(3)]
    for ticket in created:
        assign(staff["manager"], ticket["id"], staff["agent"].user_id)

    everything = inbox(staff["agent"])
    assert [item["body"] for item in everything] == [
        f"#{created[2]['number']} - Ticket 2",
        f"#{created[1]['number']} - Ticket 1",
        f"#{created[0]['number']} - Ticket 0",
    ]

    first_page = inbox(staff["agent"], limit=2)
    assert [item["id"] for item in first_page] == [item["id"] for item in everything[:2]]

    second_page = inbox(staff["agent"], limit=2, offset=2)
    assert [item["id"] for item in second_page] == [everything[2]["id"]]


def test_unread_only_filters_out_what_has_been_read(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """The flag the badge and the dropdown differ by, and the default that errs large.

    A client that forgets the flag gets the full history rather than an empty list,
    because "show me nothing" is the failure that looks like a broken feature.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    tickets = [staff["manager"].add_ticket(record["id"]) for _ in range(2)]
    for ticket in tickets:
        assign(staff["manager"], ticket["id"], staff["agent"].user_id)

    newest = inbox(staff["agent"])[0]
    response = staff["agent"].post(f"{NOTIFICATIONS}/{newest['id']}/read")
    assert response.status_code == 200, response.text

    assert len(inbox(staff["agent"])) == 2
    assert len(inbox(staff["agent"], unread_only=True)) == 1
    assert unread(staff["agent"]) == 1


def test_marking_one_read_does_not_move_an_existing_timestamp(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """A retried request must not make the read time drift.

    "When did they read it" is a question the notification centre should be able to
    answer, and a client that retries on a flaky connection would otherwise keep pushing
    the answer forward.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)
    notification_id = inbox(staff["agent"])[0]["id"]

    first = staff["agent"].post(f"{NOTIFICATIONS}/{notification_id}/read")
    second = staff["agent"].post(f"{NOTIFICATIONS}/{notification_id}/read")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["read_at"] is not None
    assert second.json()["read_at"] == first.json()["read_at"]


def test_marking_all_read_clears_the_badge_and_is_idempotent(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """One statement, and a second call that changes nothing rather than failing.

    The count returned is what changed, so the second call reporting zero is the honest
    answer and not an error — a button that says "mark all read" and then reports a
    problem because there was nothing left is a button users stop pressing.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    tickets = [staff["manager"].add_ticket(record["id"]) for _ in range(3)]
    for ticket in tickets:
        assign(staff["manager"], ticket["id"], staff["agent"].user_id)
    assert unread(staff["agent"]) == 3

    first = staff["agent"].post(f"{NOTIFICATIONS}/read-all")
    second = staff["agent"].post(f"{NOTIFICATIONS}/read-all")

    assert first.status_code == 200, first.text
    assert first.json() == {"marked_read": 3}
    assert second.json() == {"marked_read": 0}
    assert unread(staff["agent"]) == 0


def test_marking_all_read_leaves_the_other_users_alone(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """The bulk operation is scoped to the caller like every other read here.

    An `UPDATE` is the one place a missing predicate is invisible: it succeeds, it
    reports a plausible count, and the damage is a colleague's unread badge emptying.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)
    assign(staff["manager"], ticket["id"], staff["agent2"].user_id)

    assert staff["agent"].post(f"{NOTIFICATIONS}/read-all").json() == {"marked_read": 1}

    assert unread(staff["agent"]) == 0
    assert unread(staff["agent2"]) == 1


def test_a_colleagues_notification_is_not_found(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """ADR-009, asserted as an equality between two responses rather than as a status code.

    A `403` for a colleague's row and a `404` for an id nobody wrote would together answer
    "does this id exist" for anyone willing to walk a uuid space. The two responses are
    compared in full, so a future change that made the bodies differ would fail here even
    though both still returned 404.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)
    colleague = inbox(staff["agent"])[0]["id"]

    theirs = staff["agent2"].post(f"{NOTIFICATIONS}/{colleague}/read")
    absent = staff["agent2"].post(f"{NOTIFICATIONS}/{uuid.uuid4()}/read")

    assert theirs.status_code == 404
    assert theirs.json() == absent.json()
    # And it was not marked read on the way to being refused.
    assert unread(staff["agent"]) == 1


def test_an_inbox_belongs_to_one_user_even_across_roles(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """`NOTIFICATION_LIST` is held by all four roles, and none of them widens the query.

    An admin sees their own inbox and not the organization's, which is the difference
    between this and every other list endpoint in the application.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])
    assign(staff["manager"], ticket["id"], staff["agent"].user_id)

    assert len(inbox(staff["agent"])) == 1
    assert inbox(staff["admin"]) == []
    assert unread(staff["admin"]) == 0


# ---------------------------------------------------------------------------
# The ordering between the commit and the queue
# ---------------------------------------------------------------------------


def test_the_email_is_queued_only_once_the_row_is_visible(
    staff: dict[str, OrgSession],
    customer: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    sync_engine: Engine,
) -> None:
    """The task's own question, asked at the moment the task would be handed over.

    The task takes a notification id and reads the row, so a task queued before the
    commit would find nothing and return "missing" — quietly, because a row that does not
    exist is indistinguishable from one that was deleted. Asserting that the enqueue came
    after the commit is a claim about call order and is satisfied by any implementation;
    this asks whether the row is *there*, which is the thing the task needs and the thing
    a caller cannot see.

    The fixture in `tests/conftest.py` records the id instead. Replacing `delay` again
    here layers on top of it, which is why the check runs at all.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])
    visible_when_queued: list[bool] = []

    def check_then_queue(notification_id: str) -> None:
        visible_when_queued.append(stored_notifications(sync_engine, ticket["id"]) == 1)
        with sync_engine.connect() as conn:
            found = conn.execute(
                text("SELECT count(*) FROM notifications WHERE id = CAST(:id AS uuid)"),
                {"id": notification_id},
            ).scalar_one()
        visible_when_queued[-1] = visible_when_queued[-1] and found == 1

    monkeypatch.setattr(email_tasks.send_notification_email, "delay", check_then_queue)

    assign(staff["manager"], ticket["id"], staff["agent"].user_id)

    assert visible_when_queued == [True]


def test_nothing_is_queued_when_no_notification_was_written(
    staff: dict[str, OrgSession],
    customer: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the ordering claim: a silent path must not enqueue at all.

    Without this, a change that queued unconditionally and passed an empty list would
    still satisfy the test above — and the worker would be handed tasks for notifications
    that were deliberately never written.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = staff["manager"].add_ticket(record["id"])
    queued: list[str] = []
    monkeypatch.setattr(email_tasks.send_notification_email, "delay", queued.append)

    assign(staff["manager"], ticket["id"], staff["manager"].user_id)

    assert queued == []
