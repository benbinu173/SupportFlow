"""The connection registry, the publisher, and the subscriber.

Three jobs, and the module's shape follows from keeping them apart:

* **`Connection`** owns one socket's outbound side and nothing else. Everything a client
  receives is written by that connection's own writer task, which is the invariant that makes
  the next point possible.
* **`ConnectionManager`** holds the local sockets, grouped by organization, and **never
  touches a socket**. Its fan-out is `put_nowait` into per-connection queues, so a stalled TCP
  connection cannot make the fan-out wait — without that, one slow customer would freeze
  real-time delivery for every organization on the instance, which in a multi-tenant product
  is a cross-tenant denial of service rather than a performance problem.
* **`publish` / `subscribe_forever`** are the two ends of Redis Pub/Sub. Publishing is
  best-effort and never raises; subscribing reconnects forever and never gives up.

**Why fan-out goes through Redis rather than a direct in-process call.** §59 requires that
"WebSocket state must be designed with multi-instance deployment in mind", and
`docs/requirements.md` §7 forbids critical session state living only in local process memory.
The registry below *is* local process memory, and that is not a contradiction: the durable
record of every change is the `ticket_events` row and the `notifications` row, the socket
carries only the announcement that one was written, and a connection that is lost loses
latency and nothing else. What must not be local is the *event*, because the socket that needs
it may be on another instance — so it travels through Redis and every instance fans it out to
its own clients. No sticky sessions, no shared registry, and a second uvicorn worker or a
second host needs no coordination.

**The shared Redis client is not usable from a Celery task**, which this phase is the first
to discover: `event_loop.run` builds and closes a loop per invocation, so a client left in
`app/core/redis.py`'s module-level cache would be bound to a loop that no longer exists by the
next sweep. The publish functions therefore accept a client, and the worker passes one built
by `scoped_client()` — the shape `RateLimiter` already uses for the same reason.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Sequence

import structlog
from fastapi import WebSocket
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.redis import get_client
from app.core.tenancy import TenantContext
from app.models.notification import Notification
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.websocket.events import (
    ORG_CHANNEL_PATTERN,
    CloseCode,
    Envelope,
    NotificationEnvelope,
    RealtimeEventType,
    TicketEventEnvelope,
    channel_for,
    envelope_visible_to,
    organization_id_from_channel,
    parse_envelope,
    realtime_type_for,
    serialize,
)

logger = structlog.get_logger(__name__)

# How long to wait before re-subscribing after a failed connection. Fixed rather than
# exponential: the connection this retries is to a dependency the whole application already
# needs (the rate limiter and the readiness probe use the same Redis), so a long backoff
# would leave real-time updates dark well after everything else recovered. A flat second is
# slow enough not to spin on a dead server and fast enough that a restart is invisible.
_RECONNECT_SECONDS = 1.0

# The queue item is an envelope, or `None` meaning "close". A sentinel rather than a
# separate flag because the writer is blocked on `queue.get()` and has to be woken for the
# close to happen at all.
_CLOSE = None


class Connection:
    """One authenticated socket, its tenant, and its outbound queue.

    Constructed only after authentication, so `context` is never provisional. Nothing outside
    this class sends on the socket: `offer` is the only way in, and the writer task is the
    only writer, which is what keeps a fan-out loop and a close from racing over the same
    transport.
    """

    def __init__(self, websocket: WebSocket, context: TenantContext):
        self.websocket = websocket
        #: The authenticated tenant. Never provisional — a `Connection` is constructed only
        #: after `authenticate_token` and `tenant_context_for` have both returned.
        self.context = context
        self.organization_id = context.organization_id
        self.close_code: CloseCode | None = None
        self._queue: asyncio.Queue[Envelope | None] = asyncio.Queue(
            maxsize=get_settings().WS_QUEUE_MAX_DEPTH
        )

    def offer(self, envelope: Envelope) -> bool:
        """Queue one envelope for delivery. `False` if this connection is being dropped.

        Synchronous and non-blocking, deliberately: this is called from the subscriber loop,
        and anything that could wait here would make one slow client's problem into every
        other tenant's. A queue that has filled to its bound means the client has stopped
        reading — a browser tab that was suspended, a laptop that went to sleep — and the
        honest response is to disconnect it rather than to hold an unbounded backlog of
        events it will never render, because the client re-reads state over HTTP when it
        reconnects and a deeper queue would only delay that.
        """
        if self.close_code is not None:
            return False
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            self._drop_backlog()
            return False
        return True

    def _drop_backlog(self) -> None:
        """Replace whatever is queued with the close marker, and remember why.

        Discarding the backlog is the point rather than a side effect: those events describe
        state the client is about to re-read anyway, and sending them first would delay the
        close by however long the client has been failing to read.

        **The drain is a loop, and the writer is why.** Freeing one slot and appending the
        marker leaves the rest of the backlog in front of it, and the client this path exists
        for is one that has stopped reading — so the writer blocks on the first of those
        events instead of reaching the close. Against a slow *reader* that would merely be
        late; against a stopped one it is never, and the connection stays registered until the
        send timeout fires. Emptying the queue is what makes the marker the next thing the
        writer sees.
        """
        self.close_code = CloseCode.SLOW_CONSUMER
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_CLOSE)

    async def writer(self) -> None:
        """Drain the queue onto the socket until the queue says stop.

        A send is bounded by a timeout so a client that has stopped reading *without* filling
        the queue — a half-open connection, which no exception will ever be raised for — is
        also detected. `send_text` on a socket the peer has already closed raises, and the
        connection ends either way; what must not happen is this task dying silently while the
        reader keeps the registry entry alive.
        """
        settings = get_settings()
        while True:
            envelope = await self._queue.get()
            if envelope is _CLOSE:
                await self._close(CloseCode.SLOW_CONSUMER)
                return
            try:
                await asyncio.wait_for(
                    self.websocket.send_text(serialize(envelope)),
                    timeout=settings.WS_SEND_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                # Any failure here means this one connection is finished — a timeout, a
                # closed transport, a serialization problem. It is logged with the type and
                # no traceback because the event message is not in it, and the loop ends
                # rather than retrying a socket that has already failed once.
                logger.info(
                    "realtime_send_failed",
                    organization_id=str(self.organization_id),
                    error_type=type(exc).__name__,
                )
                await self._close(CloseCode.SLOW_CONSUMER)
                return

    async def _close(self, code: CloseCode) -> None:
        """Close the socket, recording the reason for the log line the handler writes."""
        self.close_code = code
        with contextlib.suppress(Exception):
            await self.websocket.close(code=code)

    async def reader(self) -> None:
        """Wait until the peer goes away. **The only thing this loop does.**

        There are no inbound verbs after authentication, and that is a decision rather than an
        omission: a client that could send commands would need a per-frame authorization path,
        and every read it wants already has an HTTP route with a capability and a row scope
        behind it. So the loop reads and discards, and its value is that it is *awaiting* — a
        disconnect is what unblocks it.

        There is no application-level heartbeat to add here. uvicorn pings every connection at
        the protocol level (`--ws-ping-interval`, 20s by default) and closes one that stops
        ponging, so a peer that vanished without a FIN is already detected one layer down, and
        this loop is what observes the resulting disconnect.
        """
        while True:
            message = await self.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return


class ConnectionManager:
    """Every socket this process is holding, grouped by the tenant that owns it.

    Grouped rather than flat because the organization is the unit of fan-out: an event for one
    tenant is offered to that tenant's sockets and no others, so the registry's shape and the
    isolation boundary are the same fact.
    """

    def __init__(self) -> None:
        self._by_organization: dict[uuid.UUID, set[Connection]] = {}

    def register(self, connection: Connection) -> None:
        self._by_organization.setdefault(connection.organization_id, set()).add(connection)

    def unregister(self, connection: Connection) -> None:
        """Forget a connection, and the organization with it once it is empty.

        Dropping the empty set matters over a long uptime: a tenant whose staff open and close
        tabs all day would otherwise leave a key behind for each one, and the dict would grow
        with churn rather than with concurrent users.
        """
        connections = self._by_organization.get(connection.organization_id)
        if connections is None:
            return
        connections.discard(connection)
        if not connections:
            del self._by_organization[connection.organization_id]

    def broadcast(self, envelope: Envelope) -> int:
        """Offer an envelope to every local socket entitled to it. Returns how many took it.

        Synchronous for the reason `offer` is: the subscriber loop must not be able to wait on
        a socket. `tuple(...)` because a connection that overflows is being closed and will
        unregister from its own task — copying is cheap and makes the iteration independent of
        when that lands.
        """
        connections = self._by_organization.get(envelope.organization_id)
        if not connections:
            return 0

        delivered = 0
        for connection in tuple(connections):
            if not envelope_visible_to(envelope, connection.context):
                continue
            if connection.offer(envelope):
                delivered += 1
        return delivered

    @property
    def total(self) -> int:
        """How many sockets this process holds. Read by the tests to prove none leak."""
        return sum(len(connections) for connections in self._by_organization.values())


#: The process's registry. Module-level because a process has one, and because a second one
#: would be a second answer to "who is connected" that no request could reconcile.
manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def _ticket_envelope(
    ticket: Ticket, event: TicketEvent, *, internal: bool
) -> TicketEventEnvelope | None:
    """Build the envelope for one timeline entry, or `None` if it is not announced.

    `internal` is passed in rather than inferred, and that is the same division
    `notify_for_event` follows: the producer knows whether the message it just wrote is
    staff-only, and the boundary decides who that excludes. Inferring it here from the event
    type would work for notes and fail for attachments, whose visibility comes from the
    message they hang off rather than from their own kind.
    """
    realtime_type = realtime_type_for(event.event_type)
    if realtime_type is None:
        return None
    return TicketEventEnvelope(
        type=realtime_type,
        organization_id=ticket.organization_id,
        ticket_id=ticket.id,
        ticket_number=ticket.number,
        from_value=event.from_value,
        to_value=event.to_value,
        assigned_agent_id=ticket.assigned_agent_id,
        # The ticket's customer, not the caller's. A manager acting on a customer's behalf
        # produces an event the customer must still see, and reading it off the context would
        # have shown it to the manager's own portal link — of which there is none.
        customer_id=ticket.customer_id,
        internal=internal,
    )


def notification_envelopes(notifications: Sequence[Notification]) -> list[NotificationEnvelope]:
    """One envelope per addressee. Public so the worker path can build without a ticket."""
    return [
        NotificationEnvelope(
            type=RealtimeEventType.NOTIFICATION_CREATED,
            organization_id=notification.organization_id,
            user_id=notification.user_id,
            notification_id=notification.id,
            notification_type=notification.notification_type,
            title=notification.title,
            ticket_id=notification.ticket_id,
        )
        for notification in notifications
    ]


async def publish(
    ticket: Ticket,
    event: TicketEvent,
    notifications: Sequence[Notification] = (),
    *,
    internal: bool = False,
    client: Redis | None = None,
) -> int:
    """Announce a committed change. **Call this after the commit.**

    The ordering is the same one `enqueue_delivery` documents and for the same reason: what a
    client is told must be a fact, and a change that has not committed is not one yet. A
    publish before the commit would push an announcement of a write that could still roll
    back, and unlike the notification row there is no later reader who could notice.

    **Never raises.** A Redis outage must not fail a request whose write already succeeded —
    the alternative is a 500 for a completed action, which is a worse lie than a missed toast.
    The failure is logged by *type* and without a traceback, following `enqueue_delivery`: a
    connection error's message embeds the Redis URL, and in production that URL carries a
    password (§4 — no secrets in logs).

    `client` exists for the caller that cannot use the shared one — a Celery task under
    `event_loop.run`, which builds and closes a loop per invocation. See the module docstring.
    """
    envelopes: list[Envelope] = []
    ticket_envelope = _ticket_envelope(ticket, event, internal=internal)
    if ticket_envelope is not None:
        envelopes.append(ticket_envelope)
    envelopes.extend(notification_envelopes(notifications))
    return await _send(envelopes, client=client)


async def publish_notifications(
    notifications: Sequence[Notification], *, client: Redis | None = None
) -> int:
    """Announce notifications with no ticket event alongside them.

    The SLA sweep's shape: an alert changes a clock rather than a field, so there is a
    `notification.created` to send and no `ticket.*` to send with it.
    """
    if not notifications:
        return 0
    return await _send(list(notification_envelopes(notifications)), client=client)


async def _send(envelopes: Sequence[Envelope], *, client: Redis | None) -> int:
    """Pipeline every envelope to the channel its own organization names.

    Grouped by organization rather than assuming one, because a batch that spans two tenants
    would otherwise be published entirely to the first one's channel. Nothing produces such a
    batch today — every producer here is scoped to one request or one organization — and the
    grouping costs one dictionary and removes the class of bug entirely.

    One round trip per organization, so a change that notified three people is one Redis
    interaction rather than four, and a call site cannot half-publish.
    """
    by_organization: dict[uuid.UUID, list[Envelope]] = {}
    for envelope in envelopes:
        by_organization.setdefault(envelope.organization_id, []).append(envelope)

    sent = 0
    try:
        redis = client or get_client()
        async with redis.pipeline(transaction=False) as pipe:
            for organization_id, group in by_organization.items():
                channel = channel_for(organization_id)
                for envelope in group:
                    pipe.publish(channel, serialize(envelope))
                sent += len(group)
            await pipe.execute()
    except (RedisError, OSError) as exc:
        logger.warning(
            "realtime_publish_failed",
            error_type=type(exc).__name__,
            detail="the change is committed; clients will see it on their next read",
        )
        return 0

    logger.debug(
        "realtime_published",
        events=sent,
        organizations=len(by_organization),
    )
    return sent


# ---------------------------------------------------------------------------
# Subscribing
# ---------------------------------------------------------------------------


async def subscribe_forever() -> None:
    """Listen for the process's whole lifetime, reconnecting forever.

    **This task must not be allowed to end.** It is started once by the application's lifespan
    and it is the only thing that turns a message on Redis into a message on a socket, so an
    exception that escaped it would leave the API serving requests normally while every
    client's real-time updates had silently stopped — a failure whose symptom is "the UI
    feels stale", which nobody reports as an outage.

    So both failure classes are absorbed and retried. A connection error is expected (Redis
    restarts, networks blip) and is logged at `warning`. Anything else is a bug, is logged with
    its traceback so it can be found, and still does not end the loop.
    """
    while True:
        try:
            await _listen()
        except asyncio.CancelledError:
            # Shutdown. Not a failure, and the only way out of this loop.
            logger.info("realtime_subscriber_stopped")
            raise
        except (RedisError, OSError) as exc:
            logger.warning("realtime_subscriber_retry", error_type=type(exc).__name__)
            await asyncio.sleep(_RECONNECT_SECONDS)
        except Exception as exc:
            logger.exception("realtime_subscriber_failed", error_type=type(exc).__name__)
            await asyncio.sleep(_RECONNECT_SECONDS)


async def _listen() -> None:
    """One subscription's lifetime: `psubscribe`, then dispatch until it breaks.

    A pattern rather than a set of channel subscriptions, because the tenants an instance
    serves are not known in advance and are not fixed — a client can connect for any
    organization at any time, and subscribing per tenant would mean re-subscribing whenever
    one appeared. Every instance therefore hears every organization's channel and forwards
    only to the sockets it holds, which is where the filtering belongs anyway: it needs the
    connection's own context, which Redis has never heard of.

    `aclose` on the way out so the connection is returned rather than left to the garbage
    collector, and suppressed because it is reached during cancellation as well as on a
    failure, and a shutdown should not fail on its way out.
    """
    pubsub = get_client().pubsub()
    try:
        await pubsub.psubscribe(ORG_CHANNEL_PATTERN)
        logger.info("realtime_subscribed", pattern=ORG_CHANNEL_PATTERN)
        async for message in pubsub.listen():
            if message.get("type") != "pmessage":
                # `psubscribe`'s channel replies arrive on the same iterator. They are not
                # messages, and dispatching one would log a spurious failure per subscribe.
                continue
            _dispatch(message["channel"], message["data"])
    finally:
        with contextlib.suppress(Exception):
            # redis-py ships annotations for `PubSub`'s async methods unevenly — `aclose` is one
            # of the ones still unannotated, so strict mode wants to know why an untyped call is
            # being made here. The answer is that there is no typed alternative: `close()` is
            # the sync-context spelling and this is an `asyncio` client.
            await pubsub.aclose()  # type: ignore[no-untyped-call]


def _dispatch(channel: bytes | str, raw: bytes | str) -> None:
    """Turn one published message into zero or more socket writes.

    Two refusals before the payload is trusted, both of which are backstops rather than the
    boundary itself — `envelope_visible_to` compares organizations again on every connection,
    so neither is load-bearing on its own:

    * the channel must name an organization this vocabulary recognises, and
    * the envelope's `organization_id` must agree with the channel it arrived on, so a message
      published to the wrong channel is dropped rather than fanned out under that channel's
      authority.

    A message that cannot be parsed is logged and skipped rather than raised. The loop's
    resilience is the module's job; a decoder's job is to say no.
    """
    organization_id = organization_id_from_channel(_text(channel))
    if organization_id is None:
        logger.warning("realtime_channel_unrecognised")
        return

    try:
        envelope = parse_envelope(_text(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("realtime_message_unreadable", error_type=type(exc).__name__)
        return

    if envelope.organization_id != organization_id:
        logger.warning(
            "realtime_channel_mismatch",
            channel_organization_id=str(organization_id),
            envelope_organization_id=str(envelope.organization_id),
        )
        return

    delivered = manager.broadcast(envelope)
    if delivered:
        logger.info(
            "realtime_delivered",
            event_type=str(envelope.type),
            organization_id=str(organization_id),
            connections=delivered,
        )


def _text(value: bytes | str) -> str:
    """redis-py hands back `bytes` unless the client decodes responses, and this one does not.

    Decoding at the edge rather than configuring the shared client: `decode_responses=True` is
    a property of the connection the rate limiter and the readiness probe also use, and
    changing it for one subscriber would be a decision made in the wrong place.
    """
    return value.decode() if isinstance(value, bytes) else value


async def serve(websocket: WebSocket, context: TenantContext) -> None:
    """Run a connection's whole life: register, pump both directions, unregister.

    Both loops are started here and both end together, because either one ending means the
    connection is over: the reader returns when the peer disconnects, and the writer returns
    when it has closed the socket for being too slow. Cancelling the sibling is what stops the
    other from holding a registry entry for a socket nothing is reading.

    Unregistering in a `finally` and not after the loops is what makes the registry honest
    under cancellation — a shutdown, or an exception inside the handler, must not leave a
    `Connection` behind that no later test or request can account for. That matters
    concretely here: the registry is process state that outlives any one request, so a leak is
    invisible until a long uptime turns it into a memory graph of dead sockets.
    """
    connection = Connection(websocket, context)
    manager.register(connection)
    logger.info(
        "realtime_connected",
        organization_id=str(context.organization_id),
        connections=manager.total,
    )

    writer = asyncio.create_task(connection.writer(), name="realtime-writer")
    try:
        # `RuntimeError` is suppressed rather than handled: Starlette raises it when `receive`
        # is called on a socket already recorded as disconnected — reachable when the writer
        # closed it a moment earlier. The connection is over either way, which is all this
        # function needs to know.
        with contextlib.suppress(RuntimeError):
            await connection.reader()
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        manager.unregister(connection)
        logger.info(
            "realtime_disconnected",
            organization_id=str(context.organization_id),
            connections=manager.total,
            close_code=connection.close_code.value if connection.close_code else None,
        )
