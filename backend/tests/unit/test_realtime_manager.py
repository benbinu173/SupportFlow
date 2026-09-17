"""The registry and its bounded queue: what happens when one client stops reading.

`tests/unit/test_realtime_events.py` covers the vocabulary and the boundary — the pure
decisions. `tests/api/test_websocket.py` and `tests/security/test_websocket.py` cover the
transport end to end. Neither can reach the case this file exists for, and it is the one the
design names as the reason the queue is bounded at all:

**A client that stops reading must not delay anybody else.** The fan-out runs inside the
application's subscriber task, which is the only thing turning Redis messages into socket
writes for every tenant on the instance. If it could block on one socket — a suspended browser
tab, a laptop that went to sleep, a peer that has stopped acknowledging — then every other
tenant's events would queue behind it. That is a cross-tenant denial of service in a
multi-tenant product, and it is the reason `offer` is synchronous, `put_nowait` is the only
write, and the queue has a bound.

Reaching that state through a real socket means filling kernel buffers before the application
queue can even start to fill, which makes an end-to-end test of it slow and machine-dependent.
It is reached here by driving `offer` directly: no socket, no event loop, no Redis. What is
being tested is a decision about a full queue, and a decision does not need a peer.
"""

import uuid

import pytest

from app.core.config import get_settings
from app.core.tenancy import TenantContext
from app.models.enums import UserRole
from app.websocket import events
from app.websocket.events import CloseCode
from app.websocket.manager import Connection, ConnectionManager

pytestmark = pytest.mark.unit

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
DEPTH = get_settings().WS_QUEUE_MAX_DEPTH


def context(
    role: UserRole = UserRole.ADMIN,
    *,
    organization_id: uuid.UUID = ORG,
    user_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
) -> TenantContext:
    return TenantContext(
        user_id=user_id or uuid.uuid4(),
        organization_id=organization_id,
        role=role,
        customer_id=customer_id,
    )


def envelope(
    *,
    organization_id: uuid.UUID = ORG,
    assigned_agent_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    internal: bool = False,
) -> events.TicketEventEnvelope:
    return events.TicketEventEnvelope(
        type=events.RealtimeEventType.TICKET_STATUS_CHANGED,
        organization_id=organization_id,
        ticket_id=uuid.uuid4(),
        ticket_number=1,
        from_value="open",
        to_value="pending",
        assigned_agent_id=assigned_agent_id,
        customer_id=customer_id,
        internal=internal,
    )


def connection(
    role: UserRole = UserRole.ADMIN,
    *,
    organization_id: uuid.UUID = ORG,
    user_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
) -> Connection:
    """A `Connection` with no socket attached.

    `websocket` is touched only by `writer`, which these tests never start, so passing `None`
    is honest rather than a shortcut — what is under test is the queue and the policy around
    it, and neither of those knows a socket exists.

    `asyncio.Queue` in Python 3.10+ needs no running loop to be constructed or to accept
    `put_nowait`, which is what makes this file synchronous.
    """
    return Connection(
        None,  # type: ignore[arg-type]
        context(role, organization_id=organization_id, user_id=user_id, customer_id=customer_id),
    )


def fill(item: Connection) -> None:
    """Take a connection to its bound, so the next `offer` is the one under test."""
    for _ in range(DEPTH):
        assert item.offer(envelope()) is True


# ---------------------------------------------------------------------------
# The bounded queue
# ---------------------------------------------------------------------------


def test_a_full_queue_drops_the_connection_rather_than_the_backlog() -> None:
    """The bound is what makes one client's silence a problem for that client alone.

    Filled to the limit, every `offer` succeeds; the next one returns `False` with the
    connection marked for closing. An unbounded queue would accept that offer, and the next,
    and the subscriber would keep accumulating events for a client that has not read a byte
    since it stopped — which is the failure the bound exists to make impossible.
    """
    item = connection()
    fill(item)

    assert item.offer(envelope()) is False
    assert item.close_code is CloseCode.SLOW_CONSUMER


def test_the_backlog_is_discarded_when_the_connection_is_dropped() -> None:
    """The queue is left holding the close marker and nothing else, so the close is not delayed.

    Discarding rather than appending is the point. The queued events describe state the client
    re-reads over HTTP the moment it reconnects, so sending them first would delay the close
    by however many of them are waiting — to a client that is by definition not reading.
    """
    item = connection()
    fill(item)
    assert item.offer(envelope()) is False

    # Exactly one item is queued, it is the close marker, and nothing is behind it.
    assert item._queue.qsize() == 1
    assert item._queue.get_nowait() is None
    assert item._queue.empty()


def test_an_offer_after_the_close_is_refused_without_touching_the_queue() -> None:
    """Once a connection is being dropped, later envelopes are not its business.

    The subscriber loop keeps running while the writer task finishes closing, so offers keep
    arriving for a moment. Each has to be refused immediately: a `put_nowait` here would land
    *after* the close marker and never be read, leaving the queue holding a dead event behind
    a sentinel the writer stops on.
    """
    item = connection()
    fill(item)
    item.offer(envelope())

    assert item.offer(envelope()) is False
    assert item._queue.qsize() == 1


def test_the_bound_is_the_configured_setting() -> None:
    """If the bound were ignored, the tests above would pass while filling a different queue."""
    assert connection()._queue.maxsize == DEPTH
    # And the setting is a real bound rather than zero or an accident of the default: a queue
    # of depth 1 would drop a client for being one message behind.
    assert DEPTH > 1


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_broadcast_reaches_only_the_sockets_entitled_to_the_envelope() -> None:
    """The fan-out is where the predicate runs, and a refused connection is not counted.

    `broadcast` returns how many took the envelope, which is what the log line reports. If it
    counted connections rather than deliveries, that number would claim "delivered" for events
    the predicate decided against — and the one diagnostic that would reveal a predicate gone
    wrong would be the thing hiding it.
    """
    registry = ConnectionManager()
    admin = connection(UserRole.ADMIN)
    agent = connection(UserRole.AGENT)
    stranger = connection(UserRole.AGENT, organization_id=OTHER_ORG)
    for item in (admin, agent, stranger):
        registry.register(item)

    owner = uuid.uuid4()
    assigned = envelope(assigned_agent_id=owner)

    # The admin holds ORGANIZATION scope and sees it; the agent is not the assignee and does
    # not; the other tenant's agent is not even looked at.
    assert registry.broadcast(assigned) == 1

    theirs = connection(UserRole.AGENT, user_id=owner)
    registry.register(theirs)
    assert registry.broadcast(assigned) == 2


def test_an_internal_envelope_is_not_counted_for_a_customer() -> None:
    """The second axis, at the fan-out: a customer owns the ticket and still may not be told."""
    registry = ConnectionManager()
    customer_id = uuid.uuid4()
    portal = connection(UserRole.CUSTOMER, customer_id=customer_id)
    registry.register(portal)

    theirs = envelope(customer_id=customer_id)
    assert registry.broadcast(theirs) == 1

    note = envelope(customer_id=customer_id, internal=True)
    assert registry.broadcast(note) == 0


def test_the_registry_is_keyed_by_organization_and_empties_as_it_goes() -> None:
    """An emptied tenant leaves no key behind, which is what keeps churn from growing the dict.

    Staff opening and closing tabs all day would otherwise leave one entry per tab ever
    opened, and the dict would grow with history rather than with concurrency.
    """
    registry = ConnectionManager()
    first = connection()
    second = connection(organization_id=OTHER_ORG)

    registry.register(first)
    registry.register(second)
    assert registry.total == 2

    registry.unregister(first)
    assert registry.total == 1

    registry.unregister(second)
    assert registry.total == 0

    # Idempotent, because a closing connection may be unregistered from more than one path.
    registry.unregister(second)
    assert registry.total == 0


def test_an_envelope_for_a_tenant_with_no_socket_is_offered_to_nobody() -> None:
    """No connections for the organization is a return of zero, not a lookup that creates one."""
    registry = ConnectionManager()
    registry.register(connection())

    assert registry.broadcast(envelope(organization_id=OTHER_ORG)) == 0
    assert registry.total == 1


def test_a_connection_registers_under_its_own_tenant() -> None:
    """The registry's key comes from the authenticated context, never from an envelope."""
    registry = ConnectionManager()
    item = connection(organization_id=OTHER_ORG)
    registry.register(item)

    assert registry._by_organization[OTHER_ORG] == {item}
    assert ORG not in registry._by_organization
