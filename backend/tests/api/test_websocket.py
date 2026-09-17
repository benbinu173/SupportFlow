"""The real-time socket, driven through a real client and a real fan-out.

Everything here goes through `TestClient.websocket_connect`, which runs a genuine ASGI
handshake, a genuine auth frame, and a genuine Redis round trip — the application's own
subscriber task is running in the same lifespan, so what these tests observe is the whole
path an event takes from a committed write to a client's socket. Nothing is stubbed.

**The suite's own sockets are the reason `WS_AUTH_TIMEOUT_SECONDS` is lowered in
`tests/conftest.py`.** A test that opens a socket and says nothing has to wait for the
server to give up, and waiting ten seconds per such test is the kind of thing that makes a
suite feel slow enough to stop running.

What this file does *not* do is assert the authorization boundary. Two tenants, an agent's
colleague, the internal note, the deactivated user, the suspended organization — those live
in `tests/security/test_websocket.py`, and the decisions themselves live in
`tests/unit/test_realtime_events.py`. This file is about the transport being wired.
"""

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import redis as redis_client
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.websocket import events
from app.websocket import manager as realtime
from tests.conftest import OrgSession

pytestmark = pytest.mark.integration

WS = "/ws"
TICKETS = "/api/v1/tickets"

#: How long to wait for the registry to reach the expected size. The registry is written on
#: the application's event loop and read here on the test's thread, so "the socket is
#: registered" is not something a `with` block can hand back — a receive proves the
#: application got as far as sending, and registration happens one statement later.
_REGISTRY_TIMEOUT_SECONDS = 5.0


@contextmanager
def open_socket(session: OrgSession) -> Iterator[WebSocketTestSession]:
    """Connect, authenticate, and hand back a socket that is ready to receive.

    The auth frame is sent and its acknowledgement drained before yielding, so a test that
    then reads an event cannot accidentally read `{"type": "authenticated"}` and conclude
    the fan-out works.
    """
    with session.client.websocket_connect(WS) as websocket:
        websocket.send_json({"type": "auth", "token": session.access_token})
        assert websocket.receive_json() == {"type": "authenticated"}
        yield websocket


def await_registry(size: int) -> None:
    """Block until the registry holds `size` connections, or fail saying what it held.

    Polling rather than a synchronization primitive, because the alternative is a hook in
    production code that exists for the test. The wait is normally under a millisecond — the
    socket is registered immediately after the acknowledgement the test already received —
    and this only ever spins when something is wrong, which is when the extra few
    milliseconds do not matter.
    """
    deadline = time.monotonic() + _REGISTRY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if realtime.manager.total == size:
            return
        time.sleep(0.01)
    raise AssertionError(f"the registry held {realtime.manager.total} connections, expected {size}")


@pytest.fixture
def org(register_org) -> OrgSession:
    return register_org(organization_name="Realtime Co")


@pytest.fixture
def customer(org: OrgSession) -> str:
    return org.add_customer(name="Ada Lovelace", email="ada@realtime.example.com")["id"]


# ---------------------------------------------------------------------------
# The handshake
# ---------------------------------------------------------------------------


def test_a_valid_token_opens_the_socket(org: OrgSession) -> None:
    """The acknowledgement, which is the only thing a client has to go on.

    Asserted as the whole body rather than by key, because the frame is a contract: a client
    that waited for `{"type": "authenticated"}` and got something else would have no way to
    tell readiness from a stray event.
    """
    with open_socket(org):
        pass


def test_a_bad_token_is_refused_with_4401(org: OrgSession) -> None:
    """A refusal is a close code, and `4401` is the one a client reads as "log in again".

    The code is the assertion. A socket that closed with `1000` after a bad token would be
    indistinguishable from the server shutting down, and the client's only reasonable
    response — refresh the token and reconnect — would be the wrong one half the time.
    """
    with org.client.websocket_connect(WS) as websocket:
        websocket.send_json({"type": "auth", "token": "not-a-token"})

        with pytest.raises(WebSocketDisconnect) as refusal:
            websocket.receive_json()

    assert refusal.value.code == 4401


def test_a_frame_that_is_not_an_auth_frame_is_refused(org: OrgSession) -> None:
    """A different refusal from a bad token, and deliberately not a more helpful one.

    A socket that said "that token is expired" and a socket that said "that token is for
    another tenant" would be an oracle: an unauthenticated caller could learn which tokens
    are real by the shape of the refusal. The client gets one code and one implication.
    """
    with org.client.websocket_connect(WS) as websocket:
        websocket.send_json({"type": "subscribe", "channel": "org:everything"})

        with pytest.raises(WebSocketDisconnect) as refusal:
            websocket.receive_json()

    assert refusal.value.code == 4401


def test_saying_nothing_closes_the_socket_with_4408(org: OrgSession) -> None:
    """The unauthenticated window is bounded, and the bound is a configured setting.

    This is the test the `WS_AUTH_TIMEOUT_SECONDS=1` line in `tests/conftest.py` exists for.
    Without a timeout an anonymous connection would be held open for as long as the peer
    cared to hold it, which is a socket-exhaustion vector that costs the attacker one TCP
    connection each — so the timeout is the control, and this is where it is proven to be
    wired rather than merely configured.
    """
    with (
        org.client.websocket_connect(WS) as websocket,
        pytest.raises(WebSocketDisconnect) as refusal,
    ):
        websocket.receive_json()

    assert refusal.value.code == 4408


# ---------------------------------------------------------------------------
# The end-to-end
# ---------------------------------------------------------------------------


def test_a_new_ticket_arrives_on_the_socket(org: OrgSession, customer: str) -> None:
    """A committed create, seen from a client that is already connected.

    The whole path in one test: the route writes the row, the service commits, the service
    publishes after the commit, Redis carries the envelope, this process's subscriber task
    receives it, the registry's predicate admits it, and the writer task puts it on the
    socket. Any one of those being unwired makes this fail.
    """
    with open_socket(org) as websocket:
        await_registry(1)
        ticket = org.add_ticket(customer, subject="The printer is on fire")

        envelope = websocket.receive_json()

    assert envelope["type"] == "ticket.created"
    assert envelope["ticket_id"] == ticket["id"]
    assert envelope["ticket_number"] == ticket["number"]
    assert envelope["to_value"] == "open"
    # Parsed rather than compared, because no HTTP response in this API exposes an
    # organization id — `UserRead` and `TicketRead` both deliberately omit it, since the
    # organization is implied by the caller's own token. The equality that matters (an
    # envelope for A is never delivered to B) needs two organizations to be meaningful and
    # lives in `tests/security/test_websocket.py`; what this asserts is that the field is
    # on the wire and well-formed, which the receiving instance needs in order to decide.
    assert isinstance(uuid.UUID(envelope["organization_id"]), uuid.UUID)


def test_a_status_change_carries_both_ends_of_the_change(org: OrgSession, customer: str) -> None:
    """`from` and `to`, which is what makes a thin envelope actionable.

    The client is told *what* changed and *from what to what*, so a toast can say
    "moved to in progress" without fetching anything — and the payload still carries no
    rendered ticket, so there is one rendering path and the socket cannot disagree with
    `GET /tickets/{id}`.
    """
    ticket = org.add_ticket(customer)
    agent = org.add_user("agent")
    org.post(f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": agent.user_id})
    org.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "in_progress"})

    with open_socket(org) as websocket:
        await_registry(1)
        response = agent.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"})
        assert response.status_code == 200, response.text

        envelope = websocket.receive_json()

    assert envelope["type"] == "ticket.status_changed"
    assert envelope["from_value"] == "in_progress"
    assert envelope["to_value"] == "resolved"


def test_an_assignment_arrives_as_both_a_ticket_event_and_a_notification(
    org: OrgSession, customer: str
) -> None:
    """Two envelopes for one action, and the order between them is the assertion.

    `publish` pipelines the ticket event and every notification event into one Redis round
    trip, so they arrive together rather than in whatever order two separate publishes
    happened to land. A client that received the toast before the ticket event would render
    a notification about a change it had not been told happened.

    Asserted as a set of types rather than as two positional reads, so the test says "both
    arrived" rather than "these two arrived in this order" — the pipelining gives ordering
    *within* the round trip, and pinning the order across it would be asserting something
    the design does not promise.
    """
    ticket = org.add_ticket(customer)
    agent = org.add_user("agent")

    with open_socket(agent) as websocket:
        await_registry(1)
        response = org.post(
            f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": agent.user_id}
        )
        assert response.status_code == 200, response.text

        first = websocket.receive_json()
        second = websocket.receive_json()

    assert {first["type"], second["type"]} == {"ticket.assigned", "notification.created"}
    notification = first if first["type"] == "notification.created" else second
    assert notification["user_id"] == agent.user_id
    assert notification["ticket_id"] == ticket["id"]
    assert notification["notification_type"] == "ticket_assigned"
    # The title is the one piece of content the envelope carries, because the alternative
    # read is a page fetch. It is server-written and already exposed by `GET /notifications`,
    # so the assertion that it is the *same* string is the assertion that the socket cannot
    # disagree with the durable record — which is what `Notification`'s docstring promises
    # when it calls itself the source of truth and the socket a delivery optimization.
    stored = agent.get("/api/v1/notifications").json()
    assert notification["title"] == stored[0]["title"]
    assert notification["title"] == "Ticket assigned to you"


def test_a_reply_and_an_internal_note_arrive_under_different_names(
    org: OrgSession, customer: str
) -> None:
    """The two message events are distinguished on the wire, and both reach staff.

    `ticket.message_added` and `ticket.note_added` come from two different routes with two
    different capabilities, and the timeline's own vocabulary separates them. The socket
    inherits that separation rather than inventing one — a single `ticket.message_added` for
    both would make an internal note indistinguishable from a customer-visible reply to any
    client that only switched on the type.
    """
    ticket = org.add_ticket(customer)

    with open_socket(org) as websocket:
        await_registry(1)
        reply = org.post(f"{TICKETS}/{ticket['id']}/messages", json={"body": "We are on it."})
        note = org.post(f"{TICKETS}/{ticket['id']}/notes", json={"body": "Refunded, see billing."})
        assert reply.status_code == 201, reply.text
        assert note.status_code == 201, note.text

        types = {websocket.receive_json()["type"] for _ in range(2)}

    assert types == {"ticket.message_added", "ticket.note_added"}


def test_the_attachment_event_names_the_file_and_never_the_body(
    org: OrgSession, customer: str
) -> None:
    """An upload announces itself, and what it announces is metadata.

    The envelope is thin in a second sense here: it carries the filename so a client can say
    "screenshot.png was added", and it carries nothing about the bytes — no storage key, no
    URL. A client that wants the file asks the download route, which is where the
    authorization for it lives.
    """
    ticket = org.add_ticket(customer)

    with open_socket(org) as websocket:
        await_registry(1)
        response = org.post(
            f"{TICKETS}/{ticket['id']}/attachments",
            files={"file": ("screenshot.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")},
        )
        assert response.status_code == 201, response.text

        envelope = websocket.receive_json()

    assert envelope["type"] == "ticket.attachment_added"
    assert envelope["ticket_id"] == ticket["id"]
    # The key is derived from the organization and a fresh uuid, so it must not be here.
    assert "storage_key" not in envelope


# ---------------------------------------------------------------------------
# The registry's lifetime
# ---------------------------------------------------------------------------


def test_the_registry_is_empty_again_once_the_socket_closes(org: OrgSession, customer: str) -> None:
    """No connection outlives its socket, asserted through the registry itself.

    The registry is process state, and a leak in it is invisible until a long uptime turns
    it into a graph of references to dead sockets — so the check is the count, before and
    after. The event in the middle is what makes it deterministic: it proves the connection
    was registered, so a `total == 0` afterwards means it was also removed rather than never
    added.

    The count is asserted to *return* to zero rather than to be zero throughout, because the
    refusal paths above open sockets that never register, and this test is about the ones
    that do.
    """
    assert realtime.manager.total == 0

    with open_socket(org) as websocket:
        await_registry(1)
        org.add_ticket(customer)
        websocket.receive_json()
        assert realtime.manager.total == 1

    assert realtime.manager.total == 0


def test_two_sockets_for_one_organization_both_receive(org: OrgSession, customer: str) -> None:
    """Fan-out within an instance, not to a single connection.

    The registry holds a *set* per organization, and the failure this guards against is a
    registry keyed by organization with a single value — which would work perfectly for one
    browser tab and drop every event for the second. Two admin sockets, one event, both
    told.
    """
    with open_socket(org) as first, open_socket(org) as second:
        await_registry(2)
        org.add_ticket(customer)

        assert first.receive_json()["type"] == "ticket.created"
        assert second.receive_json()["type"] == "ticket.created"


def test_a_client_that_has_gone_away_does_not_stop_the_others(
    org: OrgSession, customer: str
) -> None:
    """A closed socket is removed, and the next event still reaches the remaining one.

    This is the registry's `unregister` under load rather than at teardown: the first socket
    is closed *while* the second is open, and a fan-out that iterated a stale set would try
    to write to a socket nothing is reading. The second socket receiving the next event is
    what proves the first was actually removed from the set rather than merely marked.
    """
    with open_socket(org) as survivor:
        with open_socket(org) as departing:
            await_registry(2)
            departing.close()
            await_registry(1)

        org.add_ticket(customer)

        assert survivor.receive_json()["type"] == "ticket.created"


def test_a_socket_that_never_authenticated_reaches_no_registry(
    org: OrgSession, customer: str
) -> None:
    """The unauthenticated window is real, and it is empty by construction.

    A socket is accepted before it knows who is calling — that is the shape of the problem,
    not a mistake — so the property that has to hold is that nothing between `accept()` and
    the auth frame can reach a queue. This asserts it the only way that is meaningful: an
    event published while an unauthenticated socket is open is not delivered to it, and the
    registry never counted it.
    """
    with org.client.websocket_connect(WS) as anonymous:
        await_registry(0)
        org.add_ticket(customer)
        assert realtime.manager.total == 0

        # And it is still unauthenticated rather than merely uncounted: sending the event
        # above did not become its auth frame, so it is closed for saying nothing useful.
        with pytest.raises(WebSocketDisconnect) as refusal:
            anonymous.receive_json()

    assert refusal.value.code == 4408


def test_an_envelope_published_by_another_client_reaches_the_socket(
    org: OrgSession, customer: str
) -> None:
    """The subscriber task is running, and it is reading Redis rather than process memory.

    Every other test in this file drives a route and therefore cannot tell the difference
    between a working Redis fan-out and a shortcut inside the process — a dictionary the
    services appended to would look identical from the outside. This is the one test that
    rules the shortcut out: the envelope is written by a **separate Redis connection** that
    shares nothing with the application, over the same `PUBLISH` a worker in another process
    would issue. Only a live `psubscribe` in the application's lifespan can turn that into a
    message on a socket.

    It is also the phase's central claim in miniature — an event raised anywhere reaches a
    socket held anywhere else. Verification step 6 does the same thing across two processes;
    this does it across two connections, which is the part CI can check.

    The channel is read off the application's own first envelope rather than derived here.
    No HTTP response in this API exposes an organization id, so the alternative is reaching
    into the database for one — and the point of the test is that the publisher and the
    subscriber agree on a string, which is better demonstrated by taking the string from one
    of them.
    """
    with open_socket(org) as websocket:
        await_registry(1)
        ticket = org.add_ticket(customer)
        first = websocket.receive_json()
        organization_id = uuid.UUID(first["organization_id"])

        publisher = redis_client.Redis.from_url(str(get_settings().REDIS_URL))
        try:
            envelope = events.TicketEventEnvelope(
                type=events.RealtimeEventType.TICKET_STATUS_CHANGED,
                organization_id=organization_id,
                ticket_id=uuid.UUID(ticket["id"]),
                ticket_number=ticket["number"],
                from_value="open",
                to_value="pending",
            )
            receivers = publisher.publish(
                events.channel_for(organization_id), events.serialize(envelope)
            )
        finally:
            publisher.close()

        # At least one subscriber, and the arrival below is what says it was this process's.
        # A zero here means the subscription does not exist, which would otherwise look like a
        # client that is simply not being told anything.
        #
        # **`>= 1` rather than `== 1`, and the difference is a fact about Redis rather than a
        # concession.** `PUBLISH` returns the number of subscribers that took the message, and
        # channels are not namespaced by database — a running development server pointed at the
        # same Redis is subscribed to `org:*` too, so it is a second receiver (ADR-025,
        # Decision 4). Asserting exactly one would make this suite fail whenever somebody has
        # the API up, which is most of the time somebody is working on it. The count was never
        # the property under test: "the subscriber exists and this socket received it" is, and
        # the read after this line is the half that cannot be satisfied by a shortcut.
        assert receivers >= 1

        received = websocket.receive_json()

    assert received["type"] == "ticket.status_changed"
    assert received["to_value"] == "pending"


def test_an_envelope_for_an_organization_with_no_socket_is_not_delivered(
    org: OrgSession, customer: str
) -> None:
    """The channel is organization-scoped, and an instance with no socket on it stays quiet.

    Asserted through a *single read* rather than through absence of a message, because
    "nothing arrived" is also what a broken subscriber looks like. Two envelopes go onto the
    same channel in order: one addressed to an organization that does not exist, then one
    addressed to this one. If the predicate dropped the first, the read returns the second;
    if it did not, the read returns the first — so one comparison settles it without ever
    waiting on a message that is not coming.
    """
    with open_socket(org) as websocket:
        await_registry(1)
        ticket = org.add_ticket(customer)
        first = websocket.receive_json()
        organization_id = uuid.UUID(first["organization_id"])
        channel = events.channel_for(organization_id)

        publisher = redis_client.Redis.from_url(str(get_settings().REDIS_URL))
        try:
            elsewhere = events.TicketEventEnvelope(
                type=events.RealtimeEventType.TICKET_CREATED,
                organization_id=uuid.uuid4(),
                ticket_id=uuid.uuid4(),
                ticket_number=999_999,
            )
            # Delivered into the void: the channel has one subscriber, this envelope is not
            # addressed to its organization, so the predicate drops it.
            publisher.publish(channel, events.serialize(elsewhere))

            # The control: the same channel, addressed to this organization, does arrive —
            # so the socket is live and the drop above was a decision rather than an outage.
            ours = events.TicketEventEnvelope(
                type=events.RealtimeEventType.TICKET_REOPENED,
                organization_id=organization_id,
                ticket_id=uuid.UUID(ticket["id"]),
                ticket_number=ticket["number"],
            )
            publisher.publish(channel, events.serialize(ours))
        finally:
            publisher.close()

        received = websocket.receive_json()

    assert received["type"] == "ticket.reopened"
    assert received["ticket_number"] == ticket["number"]
