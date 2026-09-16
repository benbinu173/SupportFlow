"""Ticket core: raising, queueing, assigning, and the lifecycle.

This is the phase's centre of gravity, so it is the largest file. Three things are
asserted that nothing else can assert:

* **The lifecycle is enforced at the service, not merely documented.** Every edge is
  driven through the API, and every refusal is asserted with its status code — including
  the refusals that come from sending an edge to the *wrong endpoint*, which is what
  makes the route→capability mapping real rather than decorative.
* **Row scope narrows the queue per role.** The same `GET /tickets` returns three
  different sets, and no query parameter can widen one.
* **The queue is a queue.** Ticket numbers are per-organization, sequential, and
  allocated without collision — including under genuine concurrency, which is the one
  claim a single-threaded test cannot make.
"""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TICKETS, USERS, OrgSession

pytestmark = pytest.mark.integration


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Ticket Co")


@pytest.fixture
def staff(org: OrgSession) -> dict[str, OrgSession]:
    """An organization with one authenticated user per staff role."""
    return {
        "admin": org,
        "manager": org.add_user("manager", email="manager@ticketco.com"),
        "agent": org.add_user("agent", email="agent@ticketco.com"),
        "agent2": org.add_user("agent", email="agent2@ticketco.com"),
    }


@pytest.fixture
def customer(org: OrgSession) -> dict[str, object]:
    """A customer record plus a portal login linked to it.

    The link is the whole point: `RowScope.OWN` resolves through `User.customer_id`, so
    "a customer sees only their own tickets" is only testable against a login that names
    a customer the test chose.
    """
    record = org.add_customer(name="Ada Lovelace", email="ada@analytical.com")
    return {"record": record, "session": org.add_portal_user(record["id"])}


def ticket_body(customer_id: object) -> dict[str, object]:
    return {
        "subject": "Printer on fire",
        "description": "It is genuinely on fire.",
        "customer_id": str(customer_id),
    }


def event_types(session: OrgSession, ticket_id: object) -> list[str]:
    response = session.get(f"{TICKETS}/{ticket_id}/events")
    assert response.status_code == 200, response.text
    return [event["event_type"] for event in response.json()]


# ---------------------------------------------------------------------------
# Raising a ticket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager", "agent"])
def test_any_staff_role_may_raise_a_ticket_for_a_customer(
    staff: dict[str, OrgSession], customer: dict[str, object], role: str
) -> None:
    """`Create ticket` is ✓ for every role, staff and customer alike.

    Staff must name the customer: an agent taking a phone call raises the ticket on
    someone else's behalf, and a ticket with no customer could not be stored.
    """
    record = customer["record"]
    assert isinstance(record, dict)

    response = staff[role].post(TICKETS, json=ticket_body(record["id"]))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["customer_id"] == record["id"]
    assert body["status"] == "open"
    # Defaulted, not omitted: the column is non-nullable and MEDIUM is the documented
    # default.
    assert body["priority"] == "medium"
    assert body["assigned_agent_id"] is None


def test_a_ticket_is_returned_with_the_documented_fields(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)

    response = staff["admin"].post(TICKETS, json=ticket_body(record["id"]))

    assert response.status_code == 201, response.text
    assert set(response.json()) == {
        "id",
        "number",
        "customer_id",
        "assigned_agent_id",
        "subject",
        "description",
        "status",
        "priority",
        "category",
        "subcategory",
        "sentiment",
        "sentiment_confidence",
        "ai_recommended_priority",
        "ai_classification_confidence",
        "first_response_at",
        "resolved_at",
        "closed_at",
        "created_at",
        "updated_at",
        # Phase Q. Present on *every* ticket payload, not only the two read routes, and
        # populated for a staff caller — `POST /tickets` returns the ticket's clock
        # alongside the ticket, so a client that renders straight from the creation
        # response shows the countdown without a second request. Null for a portal caller,
        # which `tests/api/test_ticket_sla.py` asserts directly.
        "sla",
    }
    # The tenant is implied by the caller's token, never a field.
    assert "organization_id" not in response.text


def test_the_ai_fields_are_null_until_a_later_phase(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """Present in the schema, populated by nobody yet.

    Asserting the nulls is what keeps "the field exists and is empty" distinguishable
    from "the field was dropped from the response and nobody noticed until the UI
    shipped".
    """
    record = customer["record"]
    assert isinstance(record, dict)

    body = staff["admin"].post(TICKETS, json=ticket_body(record["id"])).json()

    assert body["sentiment"] is None
    assert body["ai_recommended_priority"] is None
    assert body["first_response_at"] is None
    assert body["resolved_at"] is None
    assert body["closed_at"] is None


def test_staff_must_name_the_customer(staff: dict[str, OrgSession]) -> None:
    """A ticket with no customer cannot be stored, and defaulting it to the caller
    would be wrong — a support agent is not the customer."""
    response = staff["admin"].post(
        TICKETS, json={"subject": "Orphan", "description": "Nobody raised this."}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_customer_id_from_another_organization_is_refused(
    register_org: Callable[..., OrgSession], staff: dict[str, OrgSession]
) -> None:
    """A 404, not a 403 — an id in another tenant is indistinguishable from one that
    does not exist, so this API cannot be used to probe for other organizations' rows."""
    other = register_org(organization_name="Other Co")
    outsider = other.add_customer(email="outsider@otherco.com")

    response = staff["admin"].post(TICKETS, json=ticket_body(outsider["id"]))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CUSTOMER_NOT_FOUND"


def test_an_unknown_customer_id_is_refused(staff: dict[str, OrgSession]) -> None:
    response = staff["admin"].post(TICKETS, json=ticket_body(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CUSTOMER_NOT_FOUND"


@pytest.mark.parametrize(
    "payload",
    [
        {"subject": "", "description": "No subject."},
        {"subject": "No description", "description": ""},
        {"subject": "x" * 501, "description": "Too long."},
    ],
)
def test_a_malformed_ticket_is_rejected(
    staff: dict[str, OrgSession], customer: dict[str, object], payload: dict[str, str]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)

    response = staff["admin"].post(TICKETS, json={**payload, "customer_id": record["id"]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_an_unknown_status_or_priority_is_rejected(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)

    response = staff["admin"].post(
        TICKETS, json={**ticket_body(record["id"]), "priority": "catastrophic"}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Priority at creation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_a_caller_who_may_change_priority_may_set_it_at_creation(
    staff: dict[str, OrgSession], customer: dict[str, object], role: str
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)

    response = staff[role].post(TICKETS, json={**ticket_body(record["id"]), "priority": "urgent"})

    assert response.status_code == 201, response.text
    assert response.json()["priority"] == "urgent"


def test_an_agent_may_not_set_priority_even_on_their_own_ticket(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """`TICKET_CHANGE_PRIORITY` is admin and manager only. An agent raising a ticket is
    in the same position as anyone else: the queue's order is not theirs to decide."""
    record = customer["record"]
    assert isinstance(record, dict)

    response = staff["agent"].post(
        TICKETS, json={**ticket_body(record["id"]), "priority": "urgent"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_a_refused_priority_is_not_silently_downgraded(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """The request is refused, not rewritten.

    A service that accepted the payload and quietly stored MEDIUM would leave the caller
    believing they had raised an urgent ticket — which is worse than an error, because
    nobody finds out until the SLA is missed.
    """
    record = customer["record"]
    assert isinstance(record, dict)

    refusal = staff["agent"].post(TICKETS, json={**ticket_body(record["id"]), "priority": "urgent"})
    assert refusal.status_code == 403

    # And nothing was created.
    listed = staff["admin"].get(TICKETS).json()
    assert [ticket for ticket in listed if ticket["subject"] == "Printer on fire"] == []


# ---------------------------------------------------------------------------
# A portal caller raising their own ticket
# ---------------------------------------------------------------------------


def test_a_customer_may_raise_their_own_ticket(customer: dict[str, object]) -> None:
    """No `customer_id` in the payload, and none is needed — it comes from the login."""
    portal = customer["session"]
    record = customer["record"]
    assert isinstance(portal, OrgSession)
    assert isinstance(record, dict)

    response = portal.post(
        TICKETS, json={"subject": "Cannot log in", "description": "Password rejected."}
    )

    assert response.status_code == 201, response.text
    assert response.json()["customer_id"] == record["id"]


def test_a_customer_may_not_raise_a_ticket_for_someone_else(
    org: OrgSession, customer: dict[str, object]
) -> None:
    """The forged field is *refused*, not ignored.

    Silently overriding it would leave the caller believing they had raised a ticket on
    another customer's behalf — and an ignored request field is a lie the API tells.
    """
    portal = customer["session"]
    other = org.add_customer(name="Someone Else", email="else@analytical.com")
    assert isinstance(portal, OrgSession)

    response = portal.post(TICKETS, json=ticket_body(other["id"]))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_customer_may_not_set_priority(customer: dict[str, object]) -> None:
    portal = customer["session"]
    assert isinstance(portal, OrgSession)

    response = portal.post(
        TICKETS,
        json={"subject": "Urgent!", "description": "I say so.", "priority": "urgent"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Ticket numbers
# ---------------------------------------------------------------------------


def test_numbering_starts_at_one_and_increments(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    admin = staff["admin"]

    numbers = [
        admin.post(
            TICKETS, json={**ticket_body(record["id"]), "subject": f"Ticket {index}"}
        ).json()["number"]
        for index in range(3)
    ]

    assert numbers == [1, 2, 3]


def test_numbering_is_per_organization(
    register_org: Callable[..., OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """Each tenant's queue starts at 1.

    A global sequence would leak the organization's age and volume to anyone who could
    see their own ticket numbers — and would tell a customer roughly how many tickets
    every other tenant had raised.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    org.add_ticket(record["id"])

    other = register_org(organization_name="Fresh Co")
    other_customer = other.add_customer(email="fresh@freshco.com")
    first_for_other = other.add_ticket(other_customer["id"])

    assert first_for_other["number"] == 1


def test_numbers_are_distinct_under_concurrent_creation(
    staff: dict[str, OrgSession], customer: dict[str, object]
) -> None:
    """The one claim a single-threaded test cannot make.

    Numbers are allocated as `MAX(number) + 1` under a per-tenant advisory lock
    (ADR-016). Without the lock, every one of these requests would read the same maximum
    and the unique index would reject all but one — so a collision shows up here as a
    500, and the assertion that there are as many distinct numbers as requests is what
    proves the lock is doing the work.

    Real threads against the real client, so these are genuinely concurrent
    transactions rather than an interleaving the test imagined.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    admin = staff["admin"]
    attempts = 8

    def raise_one(index: int) -> object:
        response = admin.post(
            TICKETS, json={**ticket_body(record["id"]), "subject": f"Concurrent {index}"}
        )
        assert response.status_code == 201, response.text
        return response.json()["number"]

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        numbers = list(pool.map(raise_one, range(attempts)))

    assert sorted(numbers) == list(range(1, attempts + 1))


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def test_an_admin_sees_every_ticket_in_the_organization(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    org.add_ticket(record["id"], subject="First")
    org.add_ticket(record["id"], subject="Second")

    subjects = {ticket["subject"] for ticket in staff["admin"].get(TICKETS).json()}

    assert subjects == {"First", "Second"}


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_a_manager_sees_the_whole_organization_too(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object], role: str
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    org.add_ticket(record["id"])

    assert len(staff[role].get(TICKETS).json()) == 1


def test_an_agent_sees_only_their_own_queue(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """The transition that is the whole phase in two requests.

    Before assignment the agent's queue is empty; after it, exactly one ticket. Nothing
    about the request changed — only the row's `assigned_agent_id`.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    agent = staff["agent"]

    assert agent.get(TICKETS).json() == []

    staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": agent.user_id}
    )

    assert [row["id"] for row in agent.get(TICKETS).json()] == [ticket["id"]]


def test_an_agent_does_not_see_a_colleagues_queue(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent2"].user_id}
    )

    assert staff["agent"].get(TICKETS).json() == []
    assert len(staff["agent2"].get(TICKETS).json()) == 1


def test_a_customer_sees_only_their_own_tickets(
    org: OrgSession, customer: dict[str, object]
) -> None:
    portal = customer["session"]
    record = customer["record"]
    assert isinstance(portal, OrgSession)
    assert isinstance(record, dict)

    mine = org.add_ticket(record["id"], subject="Mine")
    other = org.add_customer(name="Someone Else", email="else@analytical.com")
    org.add_ticket(other["id"], subject="Theirs")

    listed = portal.get(TICKETS).json()

    assert [ticket["subject"] for ticket in listed] == ["Mine"]
    assert listed[0]["id"] == mine["id"]


def test_a_query_parameter_cannot_widen_an_agents_scope(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """The classic form of this bug: a filter that grants access rather than narrowing.

    An agent asking for a colleague's queue gets an **empty page**, not that queue. The
    row scope is applied independently of the query string, so the two compose as
    "assigned to me **and** assigned to them" — which is nothing.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent2"].user_id}
    )

    response = staff["agent"].get(TICKETS, params={"assigned_agent_id": staff["agent2"].user_id})

    assert response.status_code == 200
    assert response.json() == []


def test_a_query_parameter_cannot_widen_a_customers_scope(
    org: OrgSession, customer: dict[str, object]
) -> None:
    """A customer naming another customer gets nothing, for the same reason."""
    portal = customer["session"]
    assert isinstance(portal, OrgSession)

    other = org.add_customer(name="Someone Else", email="else@analytical.com")
    org.add_ticket(other["id"], subject="Theirs")

    response = portal.get(TICKETS, params={"customer_id": other["id"]})

    assert response.status_code == 200
    assert response.json() == []


def test_the_queue_filters_by_status(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    open_ticket = org.add_ticket(record["id"], subject="Open one")
    other = org.add_ticket(record["id"], subject="Assigned one")
    admin = staff["admin"]
    admin.post(
        f"{TICKETS}/{other['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )

    found = admin.get(TICKETS, params={"status": "assigned"}).json()

    assert [ticket["id"] for ticket in found] == [other["id"]]
    assert open_ticket["status"] == "open"


def test_the_queue_filters_by_priority(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    org.add_ticket(record["id"], subject="Normal")
    urgent = org.add_ticket(record["id"], subject="Urgent", priority="urgent")

    found = staff["admin"].get(TICKETS, params={"priority": "urgent"}).json()

    assert [ticket["id"] for ticket in found] == [urgent["id"]]


def test_the_queue_filters_by_customer(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    other = org.add_customer(name="Someone Else", email="else@analytical.com")
    org.add_ticket(record["id"], subject="Mine")
    theirs = org.add_ticket(other["id"], subject="Theirs")

    found = staff["admin"].get(TICKETS, params={"customer_id": other["id"]}).json()

    assert [ticket["id"] for ticket in found] == [theirs["id"]]


def test_the_queue_is_newest_first(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    org.add_ticket(record["id"], subject="Older")
    org.add_ticket(record["id"], subject="Newer")

    subjects = [ticket["subject"] for ticket in staff["admin"].get(TICKETS).json()]

    assert subjects.index("Newer") < subjects.index("Older")


def test_the_queue_is_paginated(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    for index in range(5):
        org.add_ticket(record["id"], subject=f"Ticket {index}")
    admin = staff["admin"]

    everything = admin.get(TICKETS).json()
    first_two = admin.get(TICKETS, params={"limit": 2}).json()
    next_two = admin.get(TICKETS, params={"limit": 2, "offset": 2}).json()

    assert len(everything) == 5
    assert [*first_two, *next_two] == everything[:4]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"limit": -1}, {"offset": -1}])
def test_an_out_of_range_page_size_is_rejected(
    staff: dict[str, OrgSession], params: dict[str, int]
) -> None:
    response = staff["admin"].get(TICKETS, params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Fetching one ticket
# ---------------------------------------------------------------------------


def test_fetching_a_ticket_returns_it(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    response = staff["admin"].get(f"{TICKETS}/{ticket['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == ticket["id"]


def test_an_unknown_ticket_id_is_a_404(staff: dict[str, OrgSession]) -> None:
    response = staff["admin"].get(f"{TICKETS}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_a_malformed_ticket_id_is_rejected(staff: dict[str, OrgSession]) -> None:
    assert staff["admin"].get(f"{TICKETS}/not-a-uuid").status_code == 422


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def test_assigning_an_open_ticket_moves_it_to_assigned(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """The one lifecycle edge assignment owns.

    Doing it here rather than in `/status` is what makes "status is `ASSIGNED` with
    nobody assigned" unreachable — the two fields cannot drift apart because one write
    sets both.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    response = staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "assigned"
    assert response.json()["assigned_agent_id"] == staff["agent"].user_id


@pytest.mark.parametrize("role", ["agent", "customer"])
def test_only_a_manager_or_admin_may_assign(
    staff: dict[str, OrgSession],
    customer: dict[str, object],
    org: OrgSession,
    role: str,
) -> None:
    """`Assign ticket` is `—` for an agent, which is the distinction the scope map
    encodes: an agent works the queue, a manager decides it."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    session = staff[role] if role in staff else customer["session"]
    assert isinstance(session, OrgSession)

    response = session.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_a_manager_may_assign(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    response = staff["manager"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )

    assert response.status_code == 200, response.text


def test_assigning_to_an_agent_in_another_organization_is_refused(
    register_org: Callable[..., OrgSession],
    staff: dict[str, OrgSession],
    org: OrgSession,
    customer: dict[str, object],
) -> None:
    """Indistinguishable from assigning to an id that does not exist.

    A 404 rather than a 403, so this endpoint cannot be used to discover which user ids
    exist elsewhere in the system.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    outsider = register_org(organization_name="Other Co")

    response = staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": outsider.user_id}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_assigning_to_an_unknown_user_id_is_refused(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    response = staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": str(uuid.uuid4())}
    )

    assert response.status_code == 404


def test_reassigning_between_agents_keeps_the_status(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """Only the `OPEN → ASSIGNED` edge moves the status. A ticket already being worked
    stays being worked when it changes hands."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    admin.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )
    admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "in_progress"})

    response = admin.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent2"].user_id}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_progress"
    assert response.json()["assigned_agent_id"] == staff["agent2"].user_id


def test_assigning_the_same_agent_again_is_a_no_op(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """No write and no event.

    A timeline is a record of what happened; entries recording that nothing happened
    make it harder to read, not more complete.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    admin.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )
    before = len(event_types(admin, ticket["id"]))

    response = admin.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )

    assert response.status_code == 200
    assert len(event_types(admin, ticket["id"])) == before


def test_unassigning_an_assigned_ticket_is_refused(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """`ASSIGNED` *means* assigned, and the lifecycle has no edge back to `OPEN`.

    Clearing the agent while keeping the status would leave a ticket claiming an owner
    it does not have — which is the incoherent state the invariants exist to prevent,
    arriving from the other direction.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    admin.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )

    response = admin.post(f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": None})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TICKET_TRANSITION"


def test_unassigning_a_ticket_being_worked_is_allowed(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """From any status other than `ASSIGNED` the agent field is independent of the
    status, so clearing it is a legitimate operation."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    admin.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )
    admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "in_progress"})

    response = admin.post(f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": None})

    assert response.status_code == 200, response.text
    assert response.json()["assigned_agent_id"] is None
    assert response.json()["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_a_manager_or_admin_may_change_priority(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object], role: str
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    response = staff[role].post(f"{TICKETS}/{ticket['id']}/priority", json={"priority": "high"})

    assert response.status_code == 200, response.text
    assert response.json()["priority"] == "high"


@pytest.mark.parametrize("role", ["agent", "customer"])
def test_an_agent_or_customer_may_not_change_priority(
    staff: dict[str, OrgSession], customer: dict[str, object], org: OrgSession, role: str
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    session = staff[role] if role in staff else customer["session"]
    assert isinstance(session, OrgSession)

    response = session.post(f"{TICKETS}/{ticket['id']}/priority", json={"priority": "urgent"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_a_priority_override_does_not_destroy_the_recommendation(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """Spec §6 keeps the model's suggestion and the business decision in separate
    columns so accuracy stays measurable. Nothing populates the recommendation yet, so
    what this asserts is that the override touches only its own field."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]

    response = admin.post(f"{TICKETS}/{ticket['id']}/priority", json={"priority": "urgent"})

    assert response.status_code == 200, response.text
    assert response.json()["ai_recommended_priority"] is None


def test_setting_the_same_priority_is_a_no_op(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    before = len(event_types(admin, ticket["id"]))

    response = admin.post(f"{TICKETS}/{ticket['id']}/priority", json={"priority": "medium"})

    assert response.status_code == 200
    assert response.json()["priority"] == "medium"
    assert len(event_types(admin, ticket["id"])) == before


def test_an_unknown_priority_is_rejected(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    response = staff["admin"].post(
        f"{TICKETS}/{ticket['id']}/priority", json={"priority": "whenever"}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------


def move_to_in_progress(session: OrgSession, ticket_id: object, agent_id: str) -> None:
    """Walk a ticket to `IN_PROGRESS` the way the API requires: assign, then status."""
    session.post(f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": agent_id})
    response = session.post(f"{TICKETS}/{ticket_id}/status", json={"status": "in_progress"})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("target", "hint_fragment"),
    [
        ("assigned", "/assign"),
        ("closed", "/close"),
        ("open", "/reopen"),
    ],
)
def test_the_action_edges_are_refused_by_status_with_a_pointer(
    staff: dict[str, OrgSession],
    org: OrgSession,
    customer: dict[str, object],
    target: str,
    hint_fragment: str,
) -> None:
    """The three edges that live on their own endpoints.

    The refusal carries a hint naming the right endpoint, because "invalid transition"
    on its own leaves a client guessing — and the guess it makes is that the transition
    is illegal, when in fact it is legal and merely belongs elsewhere.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    response = staff["admin"].post(f"{TICKETS}/{ticket['id']}/status", json={"status": target})

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "INVALID_TICKET_TRANSITION"
    assert hint_fragment in body["message"]


def test_an_illegal_edge_is_refused(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """`ASSIGNED → RESOLVED` skips every state in between.

    Unlike the refusals above, there is no other endpoint that performs this edge — it
    does not exist. The message therefore carries no hint.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    admin.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": staff["agent"].user_id}
    )

    response = admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TICKET_TRANSITION"


def test_the_full_lifecycle_end_to_end(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """`OPEN → ASSIGNED → IN_PROGRESS → RESOLVED → CLOSED`, in one test.

    Walking the whole path is what catches a service that validates each edge
    correctly but fails to persist one of them — every individual assertion would pass
    against an implementation that dropped the third step.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    path = f"{TICKETS}/{ticket['id']}"

    assert (
        admin.post(path + "/assign", json={"assigned_agent_id": staff["agent"].user_id}).json()[
            "status"
        ]
        == "assigned"
    )
    assert admin.post(path + "/status", json={"status": "in_progress"}).json()["status"] == (
        "in_progress"
    )
    assert (
        admin.post(path + "/status", json={"status": "waiting_for_customer"}).json()["status"]
        == "waiting_for_customer"
    )
    assert admin.post(path + "/status", json={"status": "in_progress"}).json()["status"] == (
        "in_progress"
    )

    resolved = admin.post(path + "/status", json={"status": "resolved"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolved_at"] is not None

    closed = admin.post(path + "/close")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    assert closed.json()["closed_at"] is not None

    assert event_types(admin, ticket["id"]) == [
        "created",
        "assigned",
        "status_changed",
        "status_changed",
        "status_changed",
        "status_changed",
        "status_changed",
    ]


def test_resolving_records_the_resolution_time(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)

    response = admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"})

    assert response.status_code == 200, response.text
    assert response.json()["resolved_at"] is not None
    assert response.json()["closed_at"] is None


def test_closing_a_resolved_ticket_records_the_close_time(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)
    resolved_at = admin.post(
        f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"}
    ).json()["resolved_at"]

    closed = admin.post(f"{TICKETS}/{ticket['id']}/close").json()

    assert closed["closed_at"] is not None
    # Not overwritten: the moment someone clicked close is a different fact from when
    # the work was actually finished, and it is the second one SLA reporting needs.
    assert closed["resolved_at"] == resolved_at


def test_closing_an_unresolved_ticket_is_refused(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """`RESOLVED → CLOSED` only.

    `/close` is "confirm resolution", which is a customer agreeing the fix worked. A
    caller closing an `IN_PROGRESS` ticket is refused rather than walked through two
    transitions that would skip that confirmation entirely.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)

    response = admin.post(f"{TICKETS}/{ticket['id']}/close")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TICKET_TRANSITION"


def test_a_customer_may_confirm_the_resolution(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """`Close ticket` is ✓ for the customer role, and it is the reason the endpoint is
    separate: confirming is the customer's act, resolving is the agent's."""
    record = customer["record"]
    portal = customer["session"]
    assert isinstance(record, dict)
    assert isinstance(portal, OrgSession)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)
    admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"})

    response = portal.post(f"{TICKETS}/{ticket['id']}/close")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"


def test_reopening_returns_the_ticket_to_the_start(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """`CLOSED → OPEN`, with the terminal fields and the assignment cleared.

    `OPEN` means "nobody owns this yet", so a reopened ticket that kept its agent would
    be a state the rest of the model has no reading for.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)
    admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"})
    admin.post(f"{TICKETS}/{ticket['id']}/close")

    response = admin.post(f"{TICKETS}/{ticket['id']}/reopen")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "open"
    assert body["assigned_agent_id"] is None
    assert body["resolved_at"] is None
    assert body["closed_at"] is None


def test_a_reopened_ticket_can_be_assigned_to_the_same_agent_again(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """The regression test for the reason `reopen` clears the assignment.

    If reopening kept the agent, this ticket would be in status `OPEN` with an agent
    already set — and assigning it back to that same agent is a no-op, so it would never
    reach `ASSIGNED` again and would sit in `OPEN` forever. Clearing the field is what
    makes the cycle close.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    agent_id = staff["agent"].user_id
    move_to_in_progress(admin, ticket["id"], agent_id)
    admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"})
    admin.post(f"{TICKETS}/{ticket['id']}/close")
    admin.post(f"{TICKETS}/{ticket['id']}/reopen")

    response = admin.post(f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": agent_id})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "assigned"
    assert response.json()["assigned_agent_id"] == agent_id


def test_reopening_records_its_own_event_type(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """Not `status_changed`. Reopening is the one edge that goes backwards, and a
    timeline that recorded it as an ordinary transition would hide the cycle."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)
    admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"})
    admin.post(f"{TICKETS}/{ticket['id']}/close")

    admin.post(f"{TICKETS}/{ticket['id']}/reopen")

    assert event_types(admin, ticket["id"])[-1] == "reopened"


def test_reopening_an_open_ticket_is_refused(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    response = staff["admin"].post(f"{TICKETS}/{ticket['id']}/reopen")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TICKET_TRANSITION"


def test_closing_an_already_closed_ticket_is_refused(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)
    admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"})
    admin.post(f"{TICKETS}/{ticket['id']}/close")

    response = admin.post(f"{TICKETS}/{ticket['id']}/close")

    assert response.status_code == 409


@pytest.mark.parametrize("role", ["customer"])
def test_a_customer_may_not_move_a_ticket_through_working_states(
    staff: dict[str, OrgSession], customer: dict[str, object], org: OrgSession, role: str
) -> None:
    """`Change status` is `—` for a customer. They may confirm a resolution and reopen,
    which are the two acts that are theirs; deciding a ticket is in progress is not."""
    record = customer["record"]
    portal = customer["session"]
    assert isinstance(record, dict)
    assert isinstance(portal, OrgSession)
    ticket = org.add_ticket(record["id"])

    response = portal.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "in_progress"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_a_customer_may_reopen_their_own_closed_ticket(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """`Reopen ticket` is ✓ for a customer, scoped to their own — a fix that did not
    work is exactly the thing they need to be able to say."""
    record = customer["record"]
    portal = customer["session"]
    assert isinstance(record, dict)
    assert isinstance(portal, OrgSession)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)
    admin.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"})
    admin.post(f"{TICKETS}/{ticket['id']}/close")

    response = portal.post(f"{TICKETS}/{ticket['id']}/reopen")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "open"


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("post", "/assign", {"assigned_agent_id": None}),
        ("post", "/priority", {"priority": "high"}),
        ("post", "/status", {"status": "resolved"}),
        ("post", "/close", None),
        ("post", "/reopen", None),
    ],
)
def test_a_forged_status_is_not_accepted_as_a_starting_point(
    staff: dict[str, OrgSession],
    org: OrgSession,
    customer: dict[str, object],
    method: str,
    suffix: str,
    body: dict[str, str] | None,
) -> None:
    """The target only — the current status is read from the row.

    A client that could assert where the ticket *is* would make an illegal edge look
    legal, so the field does not exist. Sending it anyway is ignored rather than
    honoured, and the outcome is the assertion: the ticket still moves from where it
    actually was.
    """
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)

    payload = {**(body or {}), "status": "open"}

    response = admin.post(f"{TICKETS}/{ticket['id']}{suffix}", json=payload)

    # Whatever happens, the ticket is not in `OPEN` with an agent on it — which is the
    # state a honoured forged field would have produced.
    current = admin.get(f"{TICKETS}/{ticket['id']}").json()
    assert not (current["status"] == "open" and current["assigned_agent_id"] is not None)
    assert response.status_code in (200, 409, 422)


# ---------------------------------------------------------------------------
# The timeline
# ---------------------------------------------------------------------------


def test_creating_a_ticket_writes_a_created_event(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """The timeline starts at the beginning. A ticket without its `CREATED` event is a
    story whose first chapter is missing."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])

    events = staff["admin"].get(f"{TICKETS}/{ticket['id']}/events").json()

    assert [event["event_type"] for event in events] == ["created"]
    assert events[0]["to_value"] == "open"
    assert events[0]["actor_user_id"] == staff["admin"].user_id


def test_each_change_writes_its_own_event(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]

    admin.post(f"{TICKETS}/{ticket['id']}/priority", json={"priority": "high"})
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)

    assert event_types(admin, ticket["id"]) == [
        "created",
        "priority_changed",
        "assigned",
        "status_changed",
    ]


def test_an_event_records_the_values_it_moved_between(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """`from_value` and `to_value` are what make the timeline readable as a story
    rather than as a list of verbs."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)

    events = admin.get(f"{TICKETS}/{ticket['id']}/events").json()
    transition = events[-1]

    assert transition["from_value"] == "assigned"
    assert transition["to_value"] == "in_progress"


def test_the_timeline_is_oldest_first(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """A story reads forwards. The queue is newest-first because a queue is about what
    needs attention now; a history is the opposite question."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    admin = staff["admin"]
    move_to_in_progress(admin, ticket["id"], staff["agent"].user_id)

    created = [
        event["created_at"] for event in admin.get(f"{TICKETS}/{ticket['id']}/events").json()
    ]

    assert created == sorted(created)


def test_the_timeline_names_the_authenticated_actor(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """The actor is the caller, never a request field — a timeline that could claim
    somebody else did something would be worse than no timeline."""
    record = customer["record"]
    assert isinstance(record, dict)
    ticket = org.add_ticket(record["id"])
    manager = staff["manager"]

    manager.post(f"{TICKETS}/{ticket['id']}/priority", json={"priority": "high"})

    events = manager.get(f"{TICKETS}/{ticket['id']}/events").json()

    assert events[-1]["actor_user_id"] == manager.user_id


def test_a_customer_may_read_their_own_timeline(
    staff: dict[str, OrgSession], org: OrgSession, customer: dict[str, object]
) -> None:
    """Guarded by `TICKET_VIEW`, which a customer holds — for their own ticket, since
    the ticket is resolved through the same scope check as everything else."""
    record = customer["record"]
    portal = customer["session"]
    assert isinstance(record, dict)
    assert isinstance(portal, OrgSession)
    ticket = org.add_ticket(record["id"])

    response = portal.get(f"{TICKETS}/{ticket['id']}/events")

    assert response.status_code == 200, response.text
    assert [event["event_type"] for event in response.json()] == ["created"]


def test_a_customer_cannot_read_another_customers_timeline(
    org: OrgSession, customer: dict[str, object]
) -> None:
    """404 rather than 403: the ticket is out of scope, and the timeline is reachable
    exactly when its ticket is."""
    portal = customer["session"]
    assert isinstance(portal, OrgSession)
    other = org.add_customer(name="Someone Else", email="else@analytical.com")
    theirs = org.add_ticket(other["id"])

    response = portal.get(f"{TICKETS}/{theirs['id']}/events")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("post", ""),
        ("get", "/00000000-0000-0000-0000-000000000000"),
        ("get", "/00000000-0000-0000-0000-000000000000/events"),
        ("post", "/00000000-0000-0000-0000-000000000000/assign"),
        ("post", "/00000000-0000-0000-0000-000000000000/priority"),
        ("post", "/00000000-0000-0000-0000-000000000000/status"),
        ("post", "/00000000-0000-0000-0000-000000000000/close"),
        ("post", "/00000000-0000-0000-0000-000000000000/reopen"),
    ],
)
def test_every_ticket_route_requires_a_token(client: TestClient, method: str, suffix: str) -> None:
    response = getattr(client, method)(f"{TICKETS}{suffix}")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_the_user_directory_is_not_a_way_to_find_assignees(
    staff: dict[str, OrgSession],
) -> None:
    """An agent can assign nothing and list no users, so the `assigned_agent_id` they
    would need is not discoverable to them — the two capabilities compose."""
    response = staff["agent"].get(USERS)

    assert response.status_code == 403
