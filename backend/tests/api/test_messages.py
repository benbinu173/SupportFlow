"""The conversation on a ticket: replies, notes, ordering, and who sees what.

The load-bearing claim in this file is a negative one: **an internal note is absent from
a customer's read, not redacted.** A redacted entry renders as `[internal note hidden]`
and tells the customer that a private conversation happened about them and that they
cannot see it. Absence tells them nothing, which is the correct amount.

Everything here drives the real API. A thread is only reachable through its ticket, so
these tests assign the ticket first where the caller is an agent — the row scope is
applied at the ticket and a message inherits it exactly (ADR-015).
"""

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TICKETS, OrgSession

pytestmark = pytest.mark.integration

MESSAGES = "messages"
NOTES = "notes"


def messages_url(ticket_id: object) -> str:
    return f"{TICKETS}/{ticket_id}/{MESSAGES}"


def notes_url(ticket_id: object) -> str:
    return f"{TICKETS}/{ticket_id}/{NOTES}"


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Message Co")


@pytest.fixture
def staff(org: OrgSession) -> dict[str, OrgSession]:
    return {
        "admin": org,
        "manager": org.add_user("manager", email="manager@messageco.com"),
        "agent": org.add_user("agent", email="agent@messageco.com"),
        "agent2": org.add_user("agent", email="agent2@messageco.com"),
    }


@pytest.fixture
def customer(org: OrgSession) -> dict[str, object]:
    record = org.add_customer(name="Ada Lovelace", email="ada@analytical.com")
    return {"record": record, "session": org.add_portal_user(record["id"])}


@pytest.fixture
def worked_ticket(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> dict[str, object]:
    """A ticket assigned to `staff["agent"]`, so the agent can reach it at all.

    Every message route resolves its ticket first, so an unassigned ticket is a 404 for
    an agent rather than an empty thread — which is why the fixture does this rather
    than leaving each test to remember.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"], subject="Printer on fire")
    staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )
    return ticket


# ---------------------------------------------------------------------------
# Posting a reply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager", "agent"])
def test_staff_may_post_a_customer_facing_reply(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object], role: str
) -> None:
    """`Post reply` is ✓ for every role — it is the thing the product is for."""
    response = staff[role].post(messages_url(worked_ticket["id"]), json={"body": "Looking now."})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["body"] == "Looking now."
    assert body["sender_type"] == "agent"
    assert body["is_internal"] is False
    assert body["sender_user_id"] == staff[role].user_id


def test_a_customer_may_post_a_reply_on_their_own_ticket(customer: dict[str, object]) -> None:
    portal = customer["session"]
    record = customer["record"]
    assert isinstance(portal, OrgSession)
    assert isinstance(record, dict)
    ticket = portal.post(
        TICKETS, json={"subject": "Cannot log in", "description": "Password rejected."}
    ).json()

    response = portal.post(messages_url(ticket["id"]), json={"body": "Any update?"})

    assert response.status_code == 201, response.text
    assert response.json()["sender_type"] == "customer"
    assert response.json()["sender_user_id"] == portal.user_id


def test_a_reply_is_returned_with_the_documented_fields(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    response = staff["agent"].post(messages_url(worked_ticket["id"]), json={"body": "Hello."})

    assert response.status_code == 201, response.text
    assert set(response.json()) == {
        "id",
        "ticket_id",
        "sender_type",
        "sender_user_id",
        "body",
        "is_internal",
        "created_at",
    }
    # The tenant is implied by the caller's token, never a field.
    assert "organization_id" not in response.text


@pytest.mark.parametrize("payload", [{"body": ""}, {"body": "x" * 20_001}, {}])
def test_a_malformed_message_is_rejected(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object], payload: dict[str, str]
) -> None:
    response = staff["agent"].post(messages_url(worked_ticket["id"]), json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_client_cannot_choose_its_own_audience(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """`MessageCreate` has no `is_internal` field, so the reply endpoint cannot be used
    to write an internal note.

    The audience is a property of the route, and the route's capability is what decides
    it — which is the entire reason the two posting endpoints are separate. Sending the
    field anyway is ignored, and the assertion is about the outcome: nothing internal
    was created.
    """
    response = staff["agent"].post(
        messages_url(worked_ticket["id"]), json={"body": "For staff only.", "is_internal": True}
    )

    assert response.status_code == 201, response.text
    assert response.json()["is_internal"] is False


# ---------------------------------------------------------------------------
# Posting an internal note
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager", "agent"])
def test_staff_may_post_an_internal_note(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object], role: str
) -> None:
    response = staff[role].post(notes_url(worked_ticket["id"]), json={"body": "Escalating."})

    assert response.status_code == 201, response.text
    assert response.json()["is_internal"] is True
    assert response.json()["sender_type"] == "agent"


def test_a_customer_may_not_post_an_internal_note(
    customer: dict[str, object], org: OrgSession
) -> None:
    """`Post internal note` is `—` for the customer role.

    The refusal is at the route's capability, and the database has a second line of
    defence: a `CheckConstraint` refuses an internal row whose sender is the customer.
    Either alone would do; the pair means a bug in the service still cannot produce one.
    """
    portal = customer["session"]
    record = customer["record"]
    assert isinstance(portal, OrgSession)
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    response = portal.post(notes_url(ticket["id"]), json={"body": "Let me hide this."})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_a_note_is_allowed_on_a_closed_ticket(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """Unlike a reply. A note is desk bookkeeping — "refunded in full" — and never
    reaches the customer, so it cannot resurrect a conversation that has ended."""
    admin = staff["admin"]
    ticket_id = worked_ticket["id"]
    admin.post(f"{TICKETS}/{ticket_id}/status", json={"status": "in_progress"})
    admin.post(f"{TICKETS}/{ticket_id}/status", json={"status": "resolved"})
    admin.post(f"{TICKETS}/{ticket_id}/close")

    response = admin.post(notes_url(ticket_id), json={"body": "Refunded in full."})

    assert response.status_code == 201, response.text


def test_a_reply_is_refused_on_a_closed_ticket(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """A reply landing on a closed ticket is a message nobody is watching for. The
    honest answer is to make the caller reopen it, which puts the ticket back in front of
    an agent and records that it happened."""
    admin = staff["admin"]
    ticket_id = worked_ticket["id"]
    admin.post(f"{TICKETS}/{ticket_id}/status", json={"status": "in_progress"})
    admin.post(f"{TICKETS}/{ticket_id}/status", json={"status": "resolved"})
    admin.post(f"{TICKETS}/{ticket_id}/close")

    response = admin.post(messages_url(ticket_id), json={"body": "One more thing."})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_reopening_makes_a_closed_ticket_repliable_again(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """The complement of the refusal above.

    Without this, an implementation that refused every reply to a ticket that had ever
    been closed would pass the previous test while making reopening pointless.
    """
    admin = staff["admin"]
    ticket_id = worked_ticket["id"]
    admin.post(f"{TICKETS}/{ticket_id}/status", json={"status": "in_progress"})
    admin.post(f"{TICKETS}/{ticket_id}/status", json={"status": "resolved"})
    admin.post(f"{TICKETS}/{ticket_id}/close")
    admin.post(f"{TICKETS}/{ticket_id}/reopen")

    response = admin.post(messages_url(ticket_id), json={"body": "Following up."})

    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Reading the thread
# ---------------------------------------------------------------------------


def test_a_thread_is_oldest_first(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """A conversation reads forwards — the opposite of the queue, which is newest-first
    because a queue is about what needs attention now."""
    ticket_id = worked_ticket["id"]
    agent = staff["agent"]
    for body in ("First", "Second", "Third"):
        agent.post(messages_url(ticket_id), json={"body": body})

    thread = agent.get(messages_url(ticket_id)).json()

    assert [message["body"] for message in thread] == ["First", "Second", "Third"]


def test_an_agent_sees_internal_notes_inline(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """One list to a staff reader, with notes marked rather than separated."""
    ticket_id = worked_ticket["id"]
    agent = staff["agent"]
    agent.post(messages_url(ticket_id), json={"body": "Public reply."})
    agent.post(notes_url(ticket_id), json={"body": "Private note."})

    thread = agent.get(messages_url(ticket_id)).json()

    assert [(message["body"], message["is_internal"]) for message in thread] == [
        ("Public reply.", False),
        ("Private note.", True),
    ]


def test_a_customer_does_not_see_internal_notes_at_all(
    staff: dict[str, OrgSession], customer: dict[str, object], worked_ticket: dict[str, object]
) -> None:
    """**Absent, not redacted.**

    A redacted entry would render as "an internal note exists here and you cannot see
    it", which is itself information the customer portal has no business showing. The
    assertion is on the raw response text, because what must not leak is the note's
    *content* — checking only the list length would pass against an implementation that
    returned the body with a flag set wrongly.
    """
    portal = customer["session"]
    assert isinstance(portal, OrgSession)
    ticket_id = worked_ticket["id"]
    agent = staff["agent"]
    agent.post(messages_url(ticket_id), json={"body": "Public reply."})
    agent.post(notes_url(ticket_id), json={"body": "Customer is on the enterprise plan."})

    response = portal.get(messages_url(ticket_id))

    assert response.status_code == 200, response.text
    assert [message["body"] for message in response.json()] == ["Public reply."]
    assert "enterprise plan" not in response.text
    assert all(message["is_internal"] is False for message in response.json())


def test_the_customers_own_replies_appear_in_their_thread(
    staff: dict[str, OrgSession], customer: dict[str, object], worked_ticket: dict[str, object]
) -> None:
    """The complement of the test above: the filter hides staff notes, not the customer's
    own side of the conversation."""
    portal = customer["session"]
    assert isinstance(portal, OrgSession)
    ticket_id = worked_ticket["id"]
    staff["agent"].post(messages_url(ticket_id), json={"body": "Agent reply."})
    portal.post(messages_url(ticket_id), json={"body": "Customer reply."})

    thread = portal.get(messages_url(ticket_id)).json()

    assert [(message["body"], message["sender_type"]) for message in thread] == [
        ("Agent reply.", "agent"),
        ("Customer reply.", "customer"),
    ]


def test_a_manager_sees_notes_too(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """`MESSAGE_READ_INTERNAL` is held by every staff role, so supervision works."""
    ticket_id = worked_ticket["id"]
    staff["agent"].post(notes_url(ticket_id), json={"body": "A private note."})

    thread = staff["manager"].get(messages_url(ticket_id)).json()

    assert [message["body"] for message in thread] == ["A private note."]


def test_the_thread_is_paginated(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    ticket_id = worked_ticket["id"]
    agent = staff["agent"]
    for index in range(5):
        agent.post(messages_url(ticket_id), json={"body": f"Message {index}"})

    everything = agent.get(messages_url(ticket_id)).json()
    first_two = agent.get(messages_url(ticket_id), params={"limit": 2}).json()
    next_two = agent.get(messages_url(ticket_id), params={"limit": 2, "offset": 2}).json()

    assert len(everything) == 5
    assert [*first_two, *next_two] == everything[:4]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"limit": -1}, {"offset": -1}])
def test_an_out_of_range_page_size_is_rejected(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object], params: dict[str, int]
) -> None:
    response = staff["agent"].get(messages_url(worked_ticket["id"]), params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_an_empty_thread_is_an_empty_list(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """A ticket with no replies is the normal state of a new ticket, not an error."""
    response = staff["agent"].get(messages_url(worked_ticket["id"]))

    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# A message is reachable exactly when its ticket is
# ---------------------------------------------------------------------------


def test_an_agent_cannot_read_a_thread_on_a_ticket_they_do_not_own(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """Row scope is applied at the *ticket*, and the message inherits it.

    The agent is a real agent in the right organization, and the ticket is real — the
    only reason the thread is unreachable is that it is not theirs. A **404**, the same
    answer as a ticket in another organization, so the shape of the refusal does not
    disclose which of the two it was.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent2"].user_id}
    )
    staff["agent2"].post(messages_url(ticket["id"]), json={"body": "Colleague only."})

    response = staff["agent"].get(messages_url(ticket["id"]))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_an_agent_cannot_post_to_a_ticket_they_do_not_own(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """The write path applies the identical rule — a scope that only narrows reads is
    not a scope, it is a filter."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent2"].user_id}
    )

    response = staff["agent"].post(messages_url(ticket["id"]), json={"body": "Butting in."})

    assert response.status_code == 404


def test_a_customer_cannot_read_another_customers_thread(
    org: OrgSession, customer: dict[str, object]
) -> None:
    portal = customer["session"]
    assert isinstance(portal, OrgSession)
    other = org.add_customer(name="Someone Else", email="else@analytical.com")
    theirs = org.add_ticket(other["id"])
    org.add_ticket(other["id"])

    response = portal.get(messages_url(theirs["id"]))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_a_thread_in_another_organization_is_a_404(
    register_org: Callable[..., OrgSession], staff: dict[str, OrgSession]
) -> None:
    other = register_org(organization_name="Other Co")
    outsider = other.add_customer(email="outsider@otherco.com")
    their_ticket = other.add_ticket(outsider["id"])

    for response in (
        staff["admin"].get(messages_url(their_ticket["id"])),
        staff["admin"].post(messages_url(their_ticket["id"]), json={"body": "Hello."}),
        staff["admin"].post(notes_url(their_ticket["id"]), json={"body": "Hello."}),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_an_unknown_ticket_id_is_a_404_for_every_message_route(
    staff: dict[str, OrgSession],
) -> None:
    missing = uuid.uuid4()

    assert staff["admin"].get(messages_url(missing)).status_code == 404
    assert staff["admin"].post(messages_url(missing), json={"body": "x"}).status_code == 404
    assert staff["admin"].post(notes_url(missing), json={"body": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# first_response_at
# ---------------------------------------------------------------------------


def test_the_first_staff_reply_records_the_first_response_time(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """Spec §4.1's response metric, recorded at the moment it becomes true rather than
    reconstructed from the thread later — a reconstruction is only ever an estimate."""
    ticket_id = worked_ticket["id"]
    admin = staff["admin"]
    assert admin.get(f"{TICKETS}/{ticket_id}").json()["first_response_at"] is None

    staff["agent"].post(messages_url(ticket_id), json={"body": "On it."})

    assert admin.get(f"{TICKETS}/{ticket_id}").json()["first_response_at"] is not None


def test_a_customers_own_message_is_not_a_first_response(
    staff: dict[str, OrgSession], customer: dict[str, object], worked_ticket: dict[str, object]
) -> None:
    """The metric is how long the *desk* took. A customer replying to themselves must not
    satisfy it, or every ticket would look instantly answered."""
    portal = customer["session"]
    assert isinstance(portal, OrgSession)
    ticket_id = worked_ticket["id"]

    portal.post(messages_url(ticket_id), json={"body": "Adding detail."})

    assert staff["admin"].get(f"{TICKETS}/{ticket_id}").json()["first_response_at"] is None


def test_an_internal_note_is_not_a_first_response(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """A note did not reach the customer, so nothing has been responded to yet.

    This is the assertion that would fail if the timestamp were set in the shared `_post`
    helper rather than in the reply path — which is exactly the mistake the separation
    exists to prevent.
    """
    ticket_id = worked_ticket["id"]
    admin = staff["admin"]

    staff["agent"].post(notes_url(ticket_id), json={"body": "Check the billing system."})

    assert admin.get(f"{TICKETS}/{ticket_id}").json()["first_response_at"] is None


def test_the_first_response_time_is_not_overwritten_by_later_replies(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    ticket_id = worked_ticket["id"]
    admin = staff["admin"]
    agent = staff["agent"]
    agent.post(messages_url(ticket_id), json={"body": "First."})
    recorded = admin.get(f"{TICKETS}/{ticket_id}").json()["first_response_at"]

    agent.post(messages_url(ticket_id), json={"body": "Second."})

    assert admin.get(f"{TICKETS}/{ticket_id}").json()["first_response_at"] == recorded


# ---------------------------------------------------------------------------
# The timeline
# ---------------------------------------------------------------------------


def test_posting_a_reply_writes_a_message_added_event(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    ticket_id = worked_ticket["id"]
    agent = staff["agent"]

    agent.post(messages_url(ticket_id), json={"body": "A reply."})

    events = agent.get(f"{TICKETS}/{ticket_id}/events").json()
    assert [event["event_type"] for event in events] == ["created", "assigned", "message_added"]


def test_posting_a_note_writes_its_own_event_type(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """Not `message_added`. The timeline is visible to the customer, so an event type
    that did not distinguish the two would let a customer see that a private note was
    written — the same disclosure the thread filter exists to prevent."""
    ticket_id = worked_ticket["id"]
    agent = staff["agent"]

    agent.post(notes_url(ticket_id), json={"body": "A note."})

    events = agent.get(f"{TICKETS}/{ticket_id}/events").json()
    assert [event["event_type"] for event in events] == [
        "created",
        "assigned",
        "internal_note_added",
    ]


def test_a_customer_does_not_see_internal_note_events_either(
    staff: dict[str, OrgSession], customer: dict[str, object], worked_ticket: dict[str, object]
) -> None:
    """The timeline and the thread agree about who knows a note exists.

    A ticket's activity log is readable by a customer, so an `internal_note_added` entry
    would disclose — by its type and its timestamp — exactly what the thread filter is
    at pains to hide: that a private conversation happened and when. The filter is
    driven by the same `MESSAGE_READ_INTERNAL` capability that governs the thread, so
    the two cannot drift.

    Worth asserting explicitly because the failure is a *partial* one. The note's content
    never appeared in the timeline, so a test checking only for the body would pass while
    the event type still leaked the fact of the note.
    """
    portal = customer["session"]
    assert isinstance(portal, OrgSession)
    ticket_id = worked_ticket["id"]
    staff["agent"].post(notes_url(ticket_id), json={"body": "Refund approved."})

    timeline = portal.get(f"{TICKETS}/{ticket_id}/events")

    assert timeline.status_code == 200, timeline.text
    assert [event["event_type"] for event in timeline.json()] == ["created", "assigned"]
    assert "internal_note_added" not in timeline.text
    assert "Refund approved" not in timeline.text


def test_staff_still_see_internal_note_events(
    staff: dict[str, OrgSession], worked_ticket: dict[str, object]
) -> None:
    """The complement, and the reason the filter is a capability rather than a blanket
    rule: the desk's own record of what it did must stay complete."""
    ticket_id = worked_ticket["id"]
    agent = staff["agent"]
    agent.post(notes_url(ticket_id), json={"body": "Refund approved."})

    events = agent.get(f"{TICKETS}/{ticket_id}/events").json()

    assert [event["event_type"] for event in events] == [
        "created",
        "assigned",
        "internal_note_added",
    ]


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", "/messages"),
        ("post", "/messages"),
        ("post", "/notes"),
    ],
)
def test_every_message_route_requires_a_token(client: TestClient, method: str, suffix: str) -> None:
    path = f"{TICKETS}/00000000-0000-0000-0000-000000000000{suffix}"

    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
