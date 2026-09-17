"""The real-time wire vocabulary, and the boundary that decides who may see what.

**This module is the whole tenant boundary for Phase R, and it holds no services.** No
session, no `await`, no socket, no Redis — three pure decisions and the names they are
expressed in. `app/websocket/manager.py` moves bytes and `app/api/websocket.py` owns the
connection; neither decides who is allowed to see anything. That separation is what makes
§25's *"a client must not subscribe to another organization's events"* a property that can
be tested without a socket, and it is the same shape `sla_service.resolve_position` takes in
Phase Q: the rule lives in one pure function so the transport cannot form a second opinion.

**Not in `app/models/enums.py`.** That module's docstring scopes it to enumerations
"persisted as native PostgreSQL enum types", and nothing here is stored — the same argument
that put `SLATimerState` in `app/schemas/sla.py`. A realtime event exists for the few
hundred milliseconds it takes to cross Redis and reach a browser; the durable record of the
same change is the `ticket_events` row and the `notifications` row, both written before the
commit that this vocabulary is published after.

**The names are derived from the timeline's, not invented beside it.** `TicketEventType`
already says what can happen to a ticket, and `REALTIME_FOR_EVENT` below is the only place
the wire's spelling of it appears. An event the timeline does not record therefore cannot be
announced to a client, which is the property that keeps "what the UI showed" and "what the
timeline says" answerable by the same question.
"""

import json
import uuid
from collections.abc import Mapping
from enum import IntEnum, StrEnum

from pydantic import BaseModel

from app.core.permissions import (
    TICKET_SCOPE_BY_ROLE,
    Permission,
    RowScope,
    row_scope_for,
)
from app.core.tenancy import TenantContext
from app.models.enums import NotificationType, TicketEventType


class CloseCode(IntEnum):
    """Why a socket is being closed.

    The code is the only channel a WebSocket has for explaining a refusal — there is no
    response body and no status line, so `app/main.py`'s §42 error envelope cannot be used
    here and a client that only checks `onclose` needs the reason to arrive *in* the code.

    `1013` is the one standard code in the list: RFC 6455 reserves nothing in the
    4000-4999 range, so the other three follow the convention every WebSocket library
    documents (an application-specific code whose last three digits mirror the HTTP status
    it would have been) without claiming a meaning the protocol already assigned.
    """

    NORMAL = 1000
    #: The connection could not keep up and was closed rather than allowed to fall behind.
    SLOW_CONSUMER = 1013
    #: No valid credential. Also the answer to a malformed auth frame.
    UNAUTHENTICATED = 4401
    #: Authenticated, but not permitted to hold this socket.
    FORBIDDEN = 4403
    #: Nothing was sent within the configured window.
    AUTH_TIMEOUT = 4408


class RealtimeEventType(StrEnum):
    """What a client is told happened.

    Namespaced (`resource.action`) so a client can switch on the prefix, matching
    `Permission`'s convention for the same reason: the value is readable in a log line or a
    browser console without needing its Python name.
    """

    TICKET_CREATED = "ticket.created"
    TICKET_ASSIGNED = "ticket.assigned"
    TICKET_UNASSIGNED = "ticket.unassigned"
    TICKET_STATUS_CHANGED = "ticket.status_changed"
    TICKET_PRIORITY_CHANGED = "ticket.priority_changed"
    TICKET_MESSAGE_ADDED = "ticket.message_added"
    TICKET_NOTE_ADDED = "ticket.note_added"
    TICKET_ATTACHMENT_ADDED = "ticket.attachment_added"
    TICKET_REOPENED = "ticket.reopened"
    NOTIFICATION_CREATED = "notification.created"


# The timeline's vocabulary mapped onto the wire's. One table, so a new `TicketEventType`
# is either named here or named in `UNPUBLISHED_EVENT_TYPES` below — a unit test asserts
# the two partition the enum, which is what stops a later phase from adding a timeline
# entry that silently never reaches a client.
REALTIME_FOR_EVENT: Mapping[TicketEventType, RealtimeEventType] = {
    TicketEventType.CREATED: RealtimeEventType.TICKET_CREATED,
    TicketEventType.ASSIGNED: RealtimeEventType.TICKET_ASSIGNED,
    TicketEventType.UNASSIGNED: RealtimeEventType.TICKET_UNASSIGNED,
    TicketEventType.STATUS_CHANGED: RealtimeEventType.TICKET_STATUS_CHANGED,
    TicketEventType.PRIORITY_CHANGED: RealtimeEventType.TICKET_PRIORITY_CHANGED,
    TicketEventType.MESSAGE_ADDED: RealtimeEventType.TICKET_MESSAGE_ADDED,
    TicketEventType.INTERNAL_NOTE_ADDED: RealtimeEventType.TICKET_NOTE_ADDED,
    TicketEventType.ATTACHMENT_ADDED: RealtimeEventType.TICKET_ATTACHMENT_ADDED,
    TicketEventType.REOPENED: RealtimeEventType.TICKET_REOPENED,
}

# Timeline entries that produce no `ticket.*` envelope. Two reasons, and the comment names
# which is which because they expire differently:
#
#   * The SLA pair — a warning and a breach do notify somebody, and that notification is
#     published as `notification.created`. What they do not have is a ticket event worth
#     announcing: the change is to a clock, not to a field a client is rendering, and the
#     sweep touches every candidate on every pass. Phase Q's `SCHEDULED_EVENT_TYPES` is the
#     same division from the notification side.
#   * `AI_ANALYSIS_COMPLETED` — §25 lists "AI analysis completion" as a realtime event, and
#     nothing produces it yet. It is named here rather than omitted so that Phase T-W has an
#     entry to delete, and so that the partition test below does not silently pass while the
#     spec's fifth event type is unpublishable.
UNPUBLISHED_EVENT_TYPES: frozenset[TicketEventType] = frozenset(
    {
        TicketEventType.SLA_WARNING,
        TicketEventType.SLA_BREACHED,
        TicketEventType.AI_ANALYSIS_COMPLETED,
    }
)


def realtime_type_for(event_type: TicketEventType) -> RealtimeEventType | None:
    """The wire name for a timeline entry, or `None` if it is not announced.

    `None` rather than an exception: a caller publishing an SLA alert has nothing wrong with
    it, it simply has a notification to send and no ticket event to send with it.
    """
    return REALTIME_FOR_EVENT.get(event_type)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

# `org:{organization_id}`, which is not a name invented here: `docs/architecture.md` §8
# writes the flow out as "publish to Redis channel org:{organization_id}", and this is the
# code matching the document rather than the document being edited to match the code.
#
# **A channel is not a key, and Redis does not namespace channels by database.** Keys are
# separated across db 0/1/2 (ADR-022), but a `PUBLISH` on db 0 reaches a `SUBSCRIBE` on
# db 1 — so the test suite and a running development server, pointed at the same server,
# share one channel space. That is harmless here and it is worth being explicit about why:
# the isolation guarantee does not rest on the connection's database, it rests on the
# envelope's `organization_id` and on the registry being keyed by organization. The
# arrangement is stronger for it.
ORG_CHANNEL_PATTERN = "org:*"


def channel_for(organization_id: uuid.UUID) -> str:
    """The Redis channel carrying one organization's events."""
    return f"org:{organization_id}"


def organization_id_from_channel(channel: str) -> uuid.UUID | None:
    """The organization a channel names, or `None` if it is not one of ours.

    The subscriber pattern-matches, so a channel it did not expect can arrive — another
    deployment on the same Redis, or a future pattern. Parsing it here rather than reading
    `organization_id` off the payload means a mismatched channel is refused before the
    payload is even looked at. It is a **backstop, not the check**: `visible_to` compares
    organizations as well, so a forged channel name buys nothing.
    """
    prefix, _, raw = channel.partition(":")
    if prefix != "org" or not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------
# Thin on purpose. An envelope says *what changed and which ticket*, and the client re-reads
# the ticket over HTTP — so there is exactly one rendering path, and a socket payload can
# never disagree with `GET /tickets/{id}`. The alternative, pushing a rendered `TicketRead`,
# would make this module a second serializer of the same row with its own row-scope and
# permission decisions to keep in step with the routes: the same stand Phase Q took when it
# refused to re-express the SLA clock in SQL.
#
# Both models are validated on the way out *and* on the way in. The subscriber parses JSON
# that travelled through Redis, and §4's "validate all input" applies to a broker as much as
# to a request body — a malformed payload must be a logged and skipped message rather than an
# exception inside the fan-out loop.


class TicketEventEnvelope(BaseModel):
    """A change to one ticket, addressed to the organization that owns it.

    **It carries the facts the audience decision needs, and nothing more.** `assigned_agent_id`,
    `customer_id`, and `internal` are here so a receiving instance can decide locally whether
    each of its sockets may see this, without a database round trip per event per socket —
    which is what makes the fan-out O(connections) instead of O(queries). They are ids the
    caller is already entitled to see nothing of: the decision is made server-side, before the
    envelope is ever handed to a socket, so a customer's connection cannot receive an envelope
    naming somebody else's ticket in the first place.
    """

    type: RealtimeEventType
    organization_id: uuid.UUID
    ticket_id: uuid.UUID
    ticket_number: int
    #: The field this event changed, for the events that change one. Rendered for display,
    #: never parsed back — the same rule `TicketEvent.from_value` carries.
    from_value: str | None = None
    to_value: str | None = None
    assigned_agent_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    #: Set when the content behind this event is staff-only — an internal note, or an
    #: attachment on one. See `ticket_event_visible_to`.
    internal: bool = False


class NotificationEnvelope(BaseModel):
    """One person's alert, published to their organization's channel.

    Per user rather than per user-channel: a channel per person would multiply the
    subscription set by the tenant's headcount for no gain, and the filter is one comparison.
    `title` is carried because a toast wants something to render — it is server-written,
    immutable, and already exposed by `GET /notifications`, so this leaks nothing that
    fetching the page would not.
    """

    type: RealtimeEventType
    organization_id: uuid.UUID
    user_id: uuid.UUID
    notification_id: uuid.UUID
    notification_type: NotificationType
    title: str
    ticket_id: uuid.UUID | None = None


Envelope = TicketEventEnvelope | NotificationEnvelope


def serialize(envelope: Envelope) -> str:
    """The exact bytes that travel over Redis."""
    return envelope.model_dump_json()


def parse_envelope(raw: str) -> Envelope:
    """Decode one published message, or raise.

    Raising is the caller's problem to handle and not this function's: the subscriber treats
    a failure here as one unreadable message, logs it, and carries on listening. Deciding
    that here would put a policy about loop resilience inside a decoder.
    """
    payload = json.loads(raw)
    event_type = payload.get("type") if isinstance(payload, dict) else None
    if event_type == RealtimeEventType.NOTIFICATION_CREATED:
        return NotificationEnvelope.model_validate(payload)
    return TicketEventEnvelope.model_validate(payload)


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def ticket_event_visible_to(envelope: TicketEventEnvelope, context: TenantContext) -> bool:
    """Whether a socket held by `context` may be shown this event.

    Three checks, in increasing order of subtlety, and **all three are load-bearing**:

    1. **Same organization.** Redundant with the channel having delivered it, and kept
       anyway so this function is fail-closed on its own rather than depending on the
       transport having done its job first. A boundary that only holds because of something
       else is a boundary that moves the day that something else changes.

    2. **Row scope** — `TICKET_SCOPE_BY_ROLE`, the same map that governs every HTTP read:
       admin and manager reach the whole organization, an agent only tickets assigned to
       them, a customer only their own. No new authorization vocabulary, and the socket
       cannot drift from the routes because there is one table.

    3. **Internal content.** Row scope alone is *not* sufficient here, and this is the bug
       the first design of this module had: a customer owns their own ticket, so check 2
       alone would push them `ticket.note_added` for a note written about them.
       `MESSAGE_READ_INTERNAL` is the capability that already answers this — it is held by
       staff and not by customers, and it is applied *in the service* on the HTTP side
       precisely because a route cannot express "this field, for this audience".
    """
    if envelope.organization_id != context.organization_id:
        return False

    if envelope.internal and not context.has(Permission.MESSAGE_READ_INTERNAL):
        return False

    scope = row_scope_for(context.role, TICKET_SCOPE_BY_ROLE)
    if scope is RowScope.ORGANIZATION:
        return True
    if scope is RowScope.ASSIGNED:
        # `context.user_id` and not a role comparison: the agent half of "who may see this
        # ticket" is the `assigned_agent_id` column, exactly as it is on the HTTP side.
        return (
            envelope.assigned_agent_id is not None and envelope.assigned_agent_id == context.user_id
        )
    # RowScope.OWN, and the fallback for a role `row_scope_for` does not recognise — which is
    # the narrowest scope, so an unknown role sees nothing rather than everything.
    #
    # A portal account with no linked `Customer` has `customer_id is None` and therefore
    # matches nothing, which is ADR-015's reading of the same null: "reaches no rows", never
    # "reaches every row".
    return envelope.customer_id is not None and envelope.customer_id == context.customer_id


def notification_visible_to(envelope: NotificationEnvelope, context: TenantContext) -> bool:
    """Whether a socket held by `context` may be shown this notification.

    Addressee equality, which is the whole rule — and deliberately does **not** re-derive
    the audience. Who a notification was for was decided when it was written, by
    `notification_service.notify_for_event` and `notify_sla_alert`, and that decision is
    already durable in the row. Recomputing it here would be a second implementation of the
    notification policy, agreeing with the first until one of them changed — and the failure
    mode of the copy is a notification that appears in the inbox and not on the socket, or
    the reverse.
    """
    return (
        envelope.organization_id == context.organization_id and envelope.user_id == context.user_id
    )


def envelope_visible_to(envelope: Envelope, context: TenantContext) -> bool:
    """Dispatch to the boundary for this envelope's kind.

    The one entry point the connection manager uses, so the manager never has to know which
    envelope it is holding in order to decide whether to send it.
    """
    if isinstance(envelope, NotificationEnvelope):
        return notification_visible_to(envelope, context)
    return ticket_event_visible_to(envelope, context)


__all__ = [
    "ORG_CHANNEL_PATTERN",
    "REALTIME_FOR_EVENT",
    "UNPUBLISHED_EVENT_TYPES",
    "CloseCode",
    "Envelope",
    "NotificationEnvelope",
    "RealtimeEventType",
    "TicketEventEnvelope",
    "channel_for",
    "envelope_visible_to",
    "notification_visible_to",
    "organization_id_from_channel",
    "parse_envelope",
    "realtime_type_for",
    "serialize",
    "ticket_event_visible_to",
]
