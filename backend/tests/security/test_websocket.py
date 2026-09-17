"""§54's "WebSocket authorization": what a socket may be told, and who may hold one.

The socket is the one surface in this API where the authorization decision is not made by a
route. A ticket is read by asking for it and being refused, and the refusal is a status code
on a response that never contained the data. A socket is different in two ways that make a
separate suite necessary rather than redundant:

1. **The data is pushed, not requested**, so there is no request to refuse. Every envelope
   published to an organization's channel reaches *every* socket on an instance holding one
   of that organization's connections, and the only thing standing between an envelope and a
   customer's browser is a predicate. `tests/unit/test_realtime_events.py` tests that
   predicate in isolation — all four roles, both axes, no services. This file tests it where
   it actually runs: two real organizations, real sockets, real Redis, and a real event
   travelling the whole path.
2. **The audience is per event, not per caller.** An agent may be entitled to read a ticket
   and still not entitled to be *told* about it, because being told is not the same as
   reading. An assignment notification addressed to one agent travels a channel every
   colleague of theirs is listening to; the recipient is decided by `assigned_agent_id` and
   `user_id`, not by the channel.

**Absence is the hard half.** "B's socket received nothing" is also what a dead subscriber
looks like, so no test here asserts silence on its own. Each publishes a *control* envelope
afterwards — one the socket in question is entitled to — and then reads once. A single read
settles it: if the predicate let the first envelope through, the read returns that one; if it
did not, the read returns the control. Nothing sleeps and nothing waits on a message that is
not coming. The one test that reads two messages from an excluded socket is
`test_an_assignment_notifies_the_assignee_and_nobody_else`, and it says why there.

The refusals are at the bottom, each followed by the registry count. A socket that was
refused but stayed registered would be a connection holding a queue nobody may fill, and the
close code on its own would not show it.
"""

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
import redis as redis_client
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.enums import UserRole
from app.websocket import events
from app.websocket import manager as realtime
from tests.conftest import TICKETS, OrgSession

pytestmark = pytest.mark.security

WS = "/ws"

#: See `tests/api/test_websocket.py`. The registry is written on the application's loop and
#: read here, so a size is waited for rather than assumed.
_REGISTRY_TIMEOUT_SECONDS = 5.0


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, for the reason `test_tenant_isolation` gives.

    Declared here rather than at package scope: `test_route_protection.py` and
    `test_log_hygiene.py` issue no requests, and the former inspects the routing table, so
    neither should need a database to run.
    """


@pytest.fixture
def two_orgs(register_org: Callable[..., OrgSession]) -> tuple[OrgSession, OrgSession]:
    """Two unrelated organizations, each with an authenticated admin."""
    return register_org(organization_name="Northwind"), register_org(organization_name="Southwind")


@contextmanager
def open_socket(session: OrgSession) -> Iterator[WebSocketTestSession]:
    """Connect and authenticate, with the acknowledgement drained before yielding."""
    with session.client.websocket_connect(WS) as websocket:
        websocket.send_json({"type": "auth", "token": session.access_token})
        assert websocket.receive_json() == {"type": "authenticated"}
        yield websocket


def await_registry(size: int) -> None:
    """Block until the registry holds `size` connections."""
    deadline = time.monotonic() + _REGISTRY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if realtime.manager.total == size:
            return
        time.sleep(0.01)
    raise AssertionError(f"the registry held {realtime.manager.total}, expected {size}")


def assert_refused(session: OrgSession, token: str, code: int) -> None:
    """Send `token`, and require the socket to close with `code` and never be registered.

    The registry half is the reason this is a helper rather than three lines per test: a
    refusal that left a connection registered would satisfy a close-code check while leaving
    a socket on the organization's fan-out list.
    """
    before = realtime.manager.total
    with session.client.websocket_connect(WS) as websocket:
        websocket.send_json({"type": "auth", "token": token})
        with pytest.raises(WebSocketDisconnect) as refusal:
            websocket.receive_json()

    assert refusal.value.code == code
    assert realtime.manager.total == before


def publish(envelope: events.Envelope) -> None:
    """Publish one envelope through a connection the application knows nothing about.

    A raw client rather than `manager.publish`, so what is exercised is the subscriber's
    treatment of a message on the channel rather than the publisher's treatment of a model —
    the same reason the cross-process verification uses a second process.
    """
    publisher = redis_client.Redis.from_url(str(get_settings().REDIS_URL))
    try:
        publisher.publish(events.channel_for(envelope.organization_id), events.serialize(envelope))
    finally:
        publisher.close()


def control_for(organization_id: uuid.UUID, **overrides: object) -> events.TicketEventEnvelope:
    """An envelope the socket under test *is* entitled to see, used to prove it is still live.

    Every field is a fresh uuid except the organization and the ticket number, so a control
    can never be confused with the envelope the test is about — and `987_654` is a number no
    ticket in this suite receives.
    """
    values: dict[str, object] = {
        "type": events.RealtimeEventType.TICKET_CREATED,
        "organization_id": organization_id,
        "ticket_id": uuid.uuid4(),
        "ticket_number": 987_654,
    }
    values.update(overrides)
    return events.TicketEventEnvelope.model_validate(values)


def new_customer(org: OrgSession, *, local: str = "ada") -> str:
    """A customer in this organization, returning the id — which is all any caller wants.

    The address is derived from the organization's own admin address rather than written by
    hand, because `.test` is a reserved TLD that the email validator refuses, and a
    hand-written domain is how that gets rediscovered. Addresses here stay unique per test
    through the `local` part.
    """
    return str(
        org.add_customer(name="Ada Lovelace", email=f"{local}@{org.email.split('@')[-1]}")["id"]
    )


# ---------------------------------------------------------------------------
# One organization's event does not reach another's socket
# ---------------------------------------------------------------------------


def test_an_event_for_one_organization_never_reaches_the_other(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """Two tenants, two sockets, one ticket. Exactly one socket is told.

    The channel is per organization, so the failure this rules out is a subscription broader
    than its name — a `psubscribe("org:*")` that broadcast to everything it received instead
    of to the connections matching the envelope's organization. That is a one-line mistake
    with a complete cross-tenant leak behind it, and it is invisible to every HTTP test.

    The control is published to *B's own channel* and addressed to B, so it proves B's socket
    is connected, subscribed and receiving — while never once having been sent A's ticket. A
    read that returned A's event would be the leak; a read that returned the control is the
    proof there was nothing before it.

    B's organization id therefore has to come from an event on B's own channel, which is why
    B's socket is opened first and a B ticket created to drain. Taking it from A's envelope
    instead would publish the control into A's channel and hang this test rather than failing
    it — which is exactly what it did the first time it was written.
    """
    northwind, southwind = two_orgs
    northwind_customer = new_customer(northwind)

    with open_socket(southwind) as southwind_socket:
        await_registry(1)
        southwind.add_ticket(new_customer(southwind, local="south"))
        organization_id = uuid.UUID(southwind_socket.receive_json()["organization_id"])

        with open_socket(northwind) as northwind_socket:
            await_registry(2)
            ticket = northwind.add_ticket(northwind_customer, subject="Northwind only")

            envelope = northwind_socket.receive_json()
            assert envelope["type"] == "ticket.created"
            assert envelope["ticket_id"] == ticket["id"]

            publish(control_for(organization_id))
            received = southwind_socket.receive_json()

    assert received["ticket_number"] == 987_654
    assert received["ticket_id"] != ticket["id"]


def test_a_customer_on_the_wrong_organization_sees_nothing_either(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The same refusal for a portal user, whose scope is the narrowest of the four.

    `RowScope.OWN` restricts a customer to tickets whose `customer_id` is theirs, so an event
    from another tenant is refused twice over — by the organization comparison and by the row
    scope. Both are checked because they fail independently: a predicate that trusted the
    channel alone, or trusted `customer_id` alone, would pass one and not the other, and the
    second is the one that matters if the channel name is ever derived wrongly.

    `customer_id` is the stronger of the two tests here and the reason this is not simply a
    repeat of the admin case: the control envelope carries the Southwind customer's own id, so
    it is admitted by row scope and by organization both, while Northwind's ticket is refused
    by both. An implementation that matched on `customer_id` alone would deliver Northwind's
    ticket too — to the customer id it happens to share, which is nobody in this test, so it
    is the organization comparison that does the work. The pair is written to fail if either
    axis is dropped.
    """
    northwind, southwind = two_orgs
    northwind_customer = new_customer(northwind)
    southwind_customer = new_customer(southwind)
    portal = southwind.add_user(
        "customer", email="portal@southwind.example.com", customer_id=southwind_customer
    )

    with open_socket(portal) as portal_socket:
        await_registry(1)
        # The portal's own ticket, which is what gives this test Southwind's organization id
        # and drains the one envelope the socket is legitimately owed.
        theirs = southwind.add_ticket(southwind_customer)
        created = portal_socket.receive_json()
        assert created["ticket_id"] == theirs["id"]
        organization_id = uuid.UUID(created["organization_id"])

        with open_socket(northwind) as northwind_socket:
            await_registry(2)
            foreign = northwind.add_ticket(northwind_customer)
            assert northwind_socket.receive_json()["ticket_id"] == foreign["id"]

            publish(
                control_for(
                    organization_id,
                    customer_id=uuid.UUID(southwind_customer),
                )
            )
            received = portal_socket.receive_json()

    assert received["ticket_number"] == 987_654
    assert received["ticket_id"] != foreign["id"]


# ---------------------------------------------------------------------------
# An assignment reaches its assignee and nobody else
# ---------------------------------------------------------------------------


def test_an_assignment_notifies_the_assignee_and_nobody_else(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """Two actions, three envelopes, and the excluded socket reads two messages on purpose.

    `notification.created` travels the whole organization's channel, because a channel per
    person would multiply the subscription set by the headcount. The filter is `user_id` on a
    channel every colleague of the addressee is subscribed to — so the property to assert is
    not "the colleague received no notification" in isolation but "the pair the colleague
    receives is the pair addressed to them".

    **Why this test reads two messages from a socket that must have been excluded twice.**
    Assigning to `owner` produces `ticket.assigned` + `notification.created`, both of which
    the colleague is entitled to exactly neither of. Assigning to the colleague then produces
    the same two, both of which are theirs. If the first pair had leaked, the colleague's
    first two messages would be that pair — `ticket.assigned` and a notification whose
    `user_id` is `owner` — and the assertion below fails on the `user_id` rather than on a
    count. So a drained read is decisive here in a way a single control read is not, and it
    checks the notification *and* the ticket event in one pass.
    """
    northwind, _ = two_orgs
    customer = new_customer(northwind)
    owner = northwind.add_user("agent", email="owner@northwind.example.com")
    colleague = northwind.add_user("agent", email="colleague@northwind.example.com")
    ticket = northwind.add_ticket(customer)

    with open_socket(colleague) as colleague_socket:
        await_registry(1)
        northwind.post(
            f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": owner.user_id}
        )
        northwind.post(
            f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": colleague.user_id}
        )

        received = [colleague_socket.receive_json() for _ in range(2)]

    notifications = [item for item in received if item["type"] == "notification.created"]
    assert {item["type"] for item in received} == {"ticket.assigned", "notification.created"}
    assert len(notifications) == 1
    assert notifications[0]["user_id"] == colleague.user_id
    # And the ticket event is about the assignment that is theirs, which the envelope says by
    # naming them as the assignee.
    assigned = next(item for item in received if item["type"] == "ticket.assigned")
    assert assigned["assigned_agent_id"] == colleague.user_id


# ---------------------------------------------------------------------------
# Row scope, on the push path
# ---------------------------------------------------------------------------


def test_an_agent_is_not_told_about_a_ticket_that_is_not_theirs(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """`RowScope.ASSIGNED` decides the audience, and it applies to being told as well as reading.

    A manager holds `ORGANIZATION` scope and sees the whole tenant; an agent holds `ASSIGNED`
    and sees their own queue. The socket inherits that map rather than inventing a second
    one, which is what keeps a change to the scopes from silently changing only the push
    path. The failure it prevents is a socket that fans every ticket change out to every
    member of staff — an exposure that appears nowhere in the route tests, because no route
    is involved.
    """
    northwind, _ = two_orgs
    customer = new_customer(northwind)
    owner = northwind.add_user("agent", email="owner@northwind.example.com")
    other = northwind.add_user("agent", email="other@northwind.example.com")
    ticket = northwind.add_ticket(customer)

    with open_socket(other) as other_socket, open_socket(owner) as owner_socket:
        await_registry(2)
        northwind.post(
            f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": owner.user_id}
        )

        # The owner is told twice — the event and its notification — and the first of those
        # envelopes is where the organization id comes from. Nothing in this API exposes one.
        assigned = owner_socket.receive_json()
        assert assigned["type"] == "ticket.assigned"
        organization_id = uuid.UUID(assigned["organization_id"])
        owner_socket.receive_json()  # the notification

        # The owner moves the ticket on. `other` is neither assigned to it nor a manager, so
        # this is the event they must not be told about.
        moved = owner.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "in_progress"})
        assert moved.status_code == 200, moved.text

        publish(
            control_for(
                organization_id,
                ticket_id=uuid.UUID(ticket["id"]),
                assigned_agent_id=uuid.UUID(other.user_id),
            )
        )
        received = other_socket.receive_json()

    assert received["ticket_number"] == 987_654
    assert received["assigned_agent_id"] == other.user_id


def test_a_manager_is_told_about_the_same_event(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The complement of the test above: the narrowing is a scope, not a broken fan-out.

    An admin holds `ORGANIZATION` scope, so an event on a ticket assigned to somebody else
    reaches them. Without this, an implementation that simply dropped every envelope whose
    `assigned_agent_id` was not the viewer would pass the agent test and be unusable for the
    people who run the queue.
    """
    northwind, _ = two_orgs
    customer = new_customer(northwind)
    agent = northwind.add_user("agent", email="agent@northwind.example.com")
    ticket = northwind.add_ticket(customer)
    northwind.post(f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": agent.user_id})

    with open_socket(northwind) as admin_socket:
        await_registry(1)
        response = northwind.post(
            f"{TICKETS}/{ticket['id']}/status", json={"status": "in_progress"}
        )

        assert response.status_code == 200, response.text
        envelope = admin_socket.receive_json()

    assert envelope["type"] == "ticket.status_changed"
    assert envelope["ticket_id"] == ticket["id"]
    assert envelope["assigned_agent_id"] == agent.user_id


# ---------------------------------------------------------------------------
# The internal note: the leak row scope alone would have shipped
# ---------------------------------------------------------------------------


def test_a_customer_is_not_told_about_an_internal_note_on_their_own_ticket(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The case a row-scope-only boundary gets wrong, and the reason the envelope has `internal`.

    A portal user owns their ticket, so `RowScope.OWN` admits every event about it —
    including one announcing a note written for staff. `MESSAGE_READ_INTERNAL` is applied
    inside the message service today precisely because a route cannot express it: the note is
    never returned by a route a customer can reach. A socket has no route at all, so the
    boundary has to carry the fact instead. The producer states it
    (`internal=message.is_internal`) and the predicate decides who that excludes.

    The control is an envelope for the same ticket and the same customer with `internal` left
    false — the reply they *are* entitled to — so the read distinguishes the field the
    predicate actually discriminates on rather than merely "did anything arrive".

    Both sockets are open throughout, so the same two writes are proven to reach staff and
    not the customer in one test: a predicate that suppressed notes for everybody would fail
    on the staff reads.
    """
    northwind, _ = two_orgs
    customer = new_customer(northwind)
    portal = northwind.add_user(
        "customer", email="portal@northwind.example.com", customer_id=customer
    )
    ticket = northwind.add_ticket(customer)

    with open_socket(northwind) as staff_socket, open_socket(portal) as portal_socket:
        await_registry(2)
        note = northwind.post(f"{TICKETS}/{ticket['id']}/notes", json={"body": "Refund approved."})
        assert note.status_code == 201, note.text

        # Staff are told, and the envelope says why they may be.
        staff_note = staff_socket.receive_json()
        assert staff_note["type"] == "ticket.note_added"
        assert staff_note["internal"] is True
        organization_id = uuid.UUID(staff_note["organization_id"])

        # The customer is not. This read is the control that proves it.
        publish(
            control_for(
                organization_id,
                ticket_id=uuid.UUID(ticket["id"]),
                customer_id=uuid.UUID(customer),
            )
        )
        received = portal_socket.receive_json()

    assert received["ticket_number"] == 987_654
    assert received["type"] == "ticket.created"


def test_an_attachment_on_an_internal_note_is_announced_to_nobody(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The stronger case next to the note: a file on an internal note produces no event at all.

    This is not the `internal` axis applied a second time — there is nothing for the axis to
    act on. `attachment_service` writes no timeline entry for a file on an internal note, on
    the grounds that the note's own `INTERNAL_NOTE_ADDED` entry already covers it and an
    `ATTACHMENT_ADDED` entry would put a client-supplied filename on a customer-visible
    timeline for a file they cannot download. The wire vocabulary is *derived* from the
    timeline, so an event the timeline does not record has no name to be published under.

    Asserted here rather than only in the service suite because it is a security property with
    a socket attached to it: the alternative design — an `internal=True` attachment event —
    would have been defensible and would have depended on the predicate continuing to be
    right, whereas this depends on nothing being sent.

    Both sockets are proven live by a control each, published in that order so that neither
    read can be satisfied by the other's message.
    """
    northwind, _ = two_orgs
    customer = new_customer(northwind)
    portal = northwind.add_user(
        "customer", email="portal@northwind.example.com", customer_id=customer
    )
    ticket = northwind.add_ticket(customer)

    with open_socket(northwind) as staff_socket, open_socket(portal) as portal_socket:
        await_registry(2)
        note = northwind.post(f"{TICKETS}/{ticket['id']}/notes", json={"body": "Refund approved."})
        assert note.status_code == 201, note.text
        organization_id = uuid.UUID(staff_socket.receive_json()["organization_id"])

        upload = northwind.post(
            f"{TICKETS}/{ticket['id']}/attachments",
            data={"message_id": note.json()["id"]},
            files={"file": ("ledger.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")},
        )
        assert upload.status_code == 201, upload.text

        # For staff: nothing about the upload. The event vocabulary has no name for it, so the
        # next thing this socket reads is the control.
        publish(control_for(organization_id, ticket_id=uuid.UUID(ticket["id"])))
        assert staff_socket.receive_json()["ticket_number"] == 987_654

        # For the customer: nothing about the note either, since the note was internal. The
        # control here is admitted by row scope and by organization both.
        publish(
            control_for(
                organization_id,
                ticket_id=uuid.UUID(ticket["id"]),
                customer_id=uuid.UUID(customer),
            )
        )
        received = portal_socket.receive_json()

    assert received["ticket_number"] == 987_654
    # And the type is the control's, which is where the weight of this assertion is: the
    # ticket's own `ticket.created` was published before either socket existed, so nothing in
    # this test can produce that type except the control. An `attachment_added` or a
    # `note_added` here would be the leak this test exists to rule out.
    assert received["type"] == "ticket.created"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_deactivated_user_cannot_open_a_socket(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """ADR-013's promise, on the one surface where it is hardest to keep.

    The deactivation route already stops an HTTP request, because `authenticate_token`
    reloads the user and checks `is_active` on every call — so a token issued before
    deactivation stops working immediately. A socket authenticated at handshake time would
    re-check nothing for the lifetime of the connection, so the check has to live on the way
    in. This test says it does, and the successful socket first is what proves the refusal is
    about the deactivation and not about the account never having worked.
    """
    northwind, _ = two_orgs
    agent = northwind.add_user("agent", email="agent@northwind.example.com")

    with open_socket(agent):
        await_registry(1)

    response = northwind.post(f"/api/v1/users/{agent.user_id}/deactivate")
    assert response.status_code == 200, response.text

    assert_refused(agent, agent.access_token, 4401)


def test_a_token_naming_another_organization_cannot_open_a_socket(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The confused-deputy case `test_tenant_isolation` covers for HTTP, on the socket.

    The token is genuinely signed and unexpired; its `org` claim simply names a tenant the
    user's row does not belong to. This is what the second of the five checks in
    `authenticate_token` is for, and the socket reaches it through the same function rather
    than a copy of the rules (ADR-025) — which is the reason those five checks were extracted
    instead of restated.
    """
    northwind, southwind = two_orgs

    forged = create_access_token(
        uuid.UUID(northwind.user_id), uuid.UUID(southwind.user_id), UserRole.ADMIN
    )

    assert_refused(northwind, forged, 4401)
    # And the honest token still works, so the refusal is about the claim rather than the
    # user having been locked out by the attempt.
    with open_socket(northwind):
        pass


def test_a_token_for_a_user_that_does_not_exist_cannot_open_a_socket(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """A signature is not an identity: the row has to be there, and be in the same tenant."""
    northwind, _ = two_orgs

    forged = create_access_token(uuid.uuid4(), uuid.UUID(northwind.user_id), UserRole.ADMIN)

    assert_refused(northwind, forged, 4401)


def test_a_suspended_organization_cannot_open_a_socket(
    two_orgs: tuple[OrgSession, OrgSession],
    sync_engine: Engine,
) -> None:
    """A live tenant-wide kill switch, reached through the socket as through HTTP.

    Suspension is the control an operator has when an account has to stop *right now*, and it
    is enforced where every authenticated path already passes: `deps.py` loads the
    organization alongside the user and refuses a non-active one. No response exposes an
    organization id, so it is taken from an envelope the socket published — which is also
    the only way this test can name the tenant without a lookup added purely for the test.

    The socket opened *before* suspension stays open, and it is kept open here on purpose.
    This check is on the way in, exactly like the deactivation check above: an established
    connection is not re-authenticated, so suspension closes the door on new connections and
    on every request, and a client that was already connected stops on its next HTTP call —
    the same fifteen-minute token window ADR-013 describes. That is asserted rather than left
    implied, because "we suspended them and their dashboard kept updating" is not something
    the README should be the first place someone learns.

    The envelope that keeps the socket alive after suspension is published directly rather
    than through a route, because by then no route will accept a request from this tenant.
    """
    northwind, _ = two_orgs
    customer = new_customer(northwind)

    with open_socket(northwind) as websocket:
        await_registry(1)
        northwind.add_ticket(customer)
        organization_id = uuid.UUID(websocket.receive_json()["organization_id"])

        with sync_engine.begin() as connection:
            connection.execute(
                text("UPDATE organizations SET status = 'suspended' WHERE id = :id"),
                {"id": str(organization_id)},
            )

        # Still connected, and still receiving: suspension does not re-authenticate.
        publish(control_for(organization_id))
        assert websocket.receive_json()["ticket_number"] == 987_654

    assert_refused(northwind, northwind.access_token, 4401)

    # And the HTTP path agrees, which is the same check reached a different way.
    assert northwind.get("/api/v1/auth/me").status_code == 403


def test_a_frame_that_is_not_an_auth_frame_is_refused(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The unauthenticated window is empty: no other frame is treated as an instruction.

    `subscribe` is the interesting verb to try, because it is the one a naive implementation
    would support and the one that would let a caller choose its own audience — a socket that
    honoured it could join another tenant's channel with a single message.
    """
    northwind, _ = two_orgs

    with northwind.client.websocket_connect(WS) as websocket:
        websocket.send_json({"type": "subscribe", "channel": events.channel_for(uuid.uuid4())})
        with pytest.raises(WebSocketDisconnect) as refusal:
            websocket.receive_json()

    assert refusal.value.code == 4401


def test_a_frame_with_no_token_is_refused(two_orgs: tuple[OrgSession, OrgSession]) -> None:
    """A well-formed frame with nothing in it is a refusal — not a crash, and not a pass."""
    northwind, _ = two_orgs

    with northwind.client.websocket_connect(WS) as websocket:
        websocket.send_json({"type": "auth"})
        with pytest.raises(WebSocketDisconnect) as refusal:
            websocket.receive_json()

    assert refusal.value.code == 4401


def test_bytes_that_are_not_json_are_refused(two_orgs: tuple[OrgSession, OrgSession]) -> None:
    """`receive_json` on non-JSON raises a `ValueError`, which is a refusal path.

    Worth its own test because it is the one input that never reaches Pydantic: the handler
    has to catch it around the *receive*, and an implementation that only caught
    `ValidationError` would let this escape into middleware that has no response to write — a
    500 in a socket scope, which a client sees as an opaque connection failure with no code
    to act on.
    """
    northwind, _ = two_orgs

    with northwind.client.websocket_connect(WS) as websocket:
        websocket.send_text("this is not json")
        with pytest.raises(WebSocketDisconnect) as refusal:
            websocket.receive_json()

    assert refusal.value.code == 4401


def test_every_refusal_leaves_the_registry_empty(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The summary claim: a refused socket holds a queue for nobody.

    Each refusal above asserts the count around itself; this asserts the file's premise in
    one place, so a test added later that forgets the check still cannot leave the suite
    green with a leaked connection. It reads the process's registry, which is what makes it
    meaningful: nothing in this suite should still be registered.
    """
    northwind, _ = two_orgs

    assert realtime.manager.total == 0
    assert_refused(northwind, "not-a-token", 4401)
    assert_refused(northwind, create_access_token(uuid.uuid4(), uuid.uuid4(), UserRole.AGENT), 4401)
    assert realtime.manager.total == 0
