"""The real-time boundary, asserted without a socket, a session, or Redis.

Spec §25 requires that "a client must not subscribe to another organization's events", and
§8's success criteria list "cross-tenant access proven impossible by automated tests
covering … WebSocket subscriptions". This file is that proof, and it is a unit test on
purpose: `app/websocket/events.py` holds no services, so the question "may this connection
see this change?" is answerable by calling one pure function with two objects.

The alternative — asserting the boundary through a live socket — would test the same
decisions more slowly and less completely, and would fail for transport reasons whenever
the fan-out was busy. The socket suites assert that the boundary is *reached*; this file
asserts what it decides.

**The predicates are asserted per role rather than per example.** A table of roles by
scopes by organizations is the shape of the thing being tested, and a leak is a single
cell being wrong — which a handful of hand-picked cases is exactly the wrong instrument
for finding.
"""

import json
import uuid

import pytest
from pydantic import ValidationError

from app.core.permissions import (
    ROLE_PERMISSIONS,
    TICKET_SCOPE_BY_ROLE,
    Permission,
    RowScope,
    row_scope_for,
)
from app.core.tenancy import TenantContext
from app.models.enums import NotificationType, TicketEventType, UserRole
from app.websocket import events

pytestmark = pytest.mark.unit

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
TICKET = uuid.UUID("33333333-3333-3333-3333-333333333333")
AGENT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
CUSTOMER_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
USER_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")

ROLES = tuple(UserRole)


def context(
    role: UserRole,
    *,
    organization_id: uuid.UUID = ORG,
    user_id: uuid.UUID = USER_ID,
    customer_id: uuid.UUID | None = None,
) -> TenantContext:
    """A context for one role.

    Built through the real dataclass rather than a stand-in, because the permissions a role
    carries are half of what the predicates read — a fake context with a hand-picked
    permission set would be testing the fake.
    """
    return TenantContext(
        user_id=user_id,
        organization_id=organization_id,
        role=role,
        customer_id=customer_id,
    )


def ticket_envelope(
    *,
    type: events.RealtimeEventType = events.RealtimeEventType.TICKET_STATUS_CHANGED,
    organization_id: uuid.UUID = ORG,
    assigned_agent_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    internal: bool = False,
) -> events.TicketEventEnvelope:
    return events.TicketEventEnvelope(
        type=type,
        organization_id=organization_id,
        ticket_id=TICKET,
        ticket_number=42,
        from_value="open",
        to_value="in_progress",
        assigned_agent_id=assigned_agent_id,
        customer_id=customer_id,
        internal=internal,
    )


def notification_envelope(
    *,
    organization_id: uuid.UUID = ORG,
    user_id: uuid.UUID = USER_ID,
    ticket_id: uuid.UUID | None = TICKET,
) -> events.NotificationEnvelope:
    return events.NotificationEnvelope(
        type=events.RealtimeEventType.NOTIFICATION_CREATED,
        organization_id=organization_id,
        user_id=user_id,
        notification_id=uuid.uuid4(),
        notification_type=NotificationType.TICKET_ASSIGNED,
        title="Ticket #42 was assigned to you",
        ticket_id=ticket_id,
    )


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_every_timeline_event_is_either_announced_or_named_as_silent() -> None:
    """The mapping and the silent set partition `TicketEventType`.

    `notification_service` makes the same claim about its own two sets, and for the same
    reason: a `TicketEventType` member added in a later phase must be a deliberate
    decision about whether a client hears about it, not an omission nobody notices.

    A new member fails this test twice over — it is neither in `REALTIME_FOR_EVENT` nor in
    `UNPUBLISHED_EVENT_TYPES` — and the fix is to name it in one of them, which is exactly
    the moment someone asks which it should be.
    """
    announced = set(events.REALTIME_FOR_EVENT)
    silent = set(events.UNPUBLISHED_EVENT_TYPES)

    assert announced & silent == set(), "an event is both announced and declared unpublished"
    assert announced | silent == set(TicketEventType), (
        f"unnamed: {sorted(set(TicketEventType) - announced - silent, key=str)}"
    )


def test_the_wire_names_are_unique_and_namespaced() -> None:
    """Two timeline entries must not map to the same name, and every name is `x.y`.

    A collision would make the vocabulary ambiguous in exactly the place it matters — a
    client switching on `event.type` would have two different changes arriving under one
    name, and no test that asserted a single mapping would notice.
    """
    names = list(events.REALTIME_FOR_EVENT.values())

    assert len(names) == len(set(names)), "two timeline events share a wire name"
    for name in names:
        assert name.count(".") == 1, f"{name} is not namespaced"
        assert str(name) == name.value


def test_unpublished_events_are_reported_as_unpublishable() -> None:
    """`realtime_type_for` returns `None` rather than raising, and says which are which.

    `None` is the honest answer for the SLA pair: the sweep has a notification to send and
    no ticket event to send with it, which is a normal call rather than an error. The
    second half asserts the lookup agrees with the table, so a caller cannot get a name for
    something declared silent.
    """
    assert events.realtime_type_for(TicketEventType.SLA_WARNING) is None
    assert events.realtime_type_for(TicketEventType.AI_ANALYSIS_COMPLETED) is None

    for event_type, expected in events.REALTIME_FOR_EVENT.items():
        assert events.realtime_type_for(event_type) is expected


def test_every_role_can_hold_a_socket() -> None:
    """The socket's capability floor, asserted against the matrix rather than assumed.

    `app/api/websocket.py` refuses a connection whose context lacks `TICKET_VIEW` and
    `NOTIFICATION_LIST`. §3 gives both to all four roles, so today this is a statement
    about what the channel carries rather than a narrowing of who may connect.

    It is worth a test anyway, because the day that stops being true the consequence is
    silent: a role would lose its real-time updates and nothing else in the suite would
    notice, since every route the role can still reach would keep working.
    """
    for role in ROLES:
        held = ROLE_PERMISSIONS[role]
        assert Permission.TICKET_VIEW in held, f"{role} could not hold a socket"
        assert Permission.NOTIFICATION_LIST in held, f"{role} could not hold a socket"


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_a_channel_names_its_organization_and_round_trips() -> None:
    """The channel format, and the parse that has to agree with it.

    `docs/architecture.md` §8 writes the channel as `org:{organization_id}`, so this is the
    code being checked against the document rather than the other way round. The
    round trip is what makes the subscriber's two-step — parse the channel, then compare it
    to the payload — possible at all.
    """
    channel = events.channel_for(ORG)

    assert channel == f"org:{ORG}"
    assert events.organization_id_from_channel(channel) == ORG


@pytest.mark.parametrize(
    "channel",
    [
        "",
        "org",
        "org:",
        "org:not-a-uuid",
        # A different prefix entirely: another deployment's channels on the same Redis, or
        # a future pattern this build does not know about.
        "orgs:11111111-1111-1111-1111-111111111111",
        "tenant:11111111-1111-1111-1111-111111111111",
    ],
)
def test_a_channel_that_is_not_ours_is_refused(channel: str) -> None:
    """`None` rather than an exception, because an unrecognised channel is not an error.

    The subscriber pattern-matches, so a channel it did not expect can arrive from anywhere
    and simply is not ours. Raising would turn a stray message on a shared Redis into a
    crash in the fan-out loop.
    """
    assert events.organization_id_from_channel(channel) is None


def test_the_pattern_covers_every_channel_the_application_publishes_to() -> None:
    """The subscription pattern and the channel format cannot drift apart.

    `psubscribe("org:*")` is a literal, and `channel_for` builds a string. If one changed
    without the other the subscriber would keep running, connected, receiving nothing — the
    failure mode with no symptom. Checked here rather than through Redis so it holds whether
    or not a server is running.
    """
    prefix, _, suffix = events.ORG_CHANNEL_PATTERN.partition("*")

    assert prefix == "org:"
    assert suffix == ""
    assert events.channel_for(ORG).startswith(prefix)


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def test_an_envelope_survives_the_wire() -> None:
    """Serialized and parsed back, field for field.

    This is the only journey an envelope makes — out through Redis and in through another
    process — and the parse validates with the same model the publisher built, which is what
    makes a payload that arrived over a broker subject to §4's "validate all input".
    """
    envelope = ticket_envelope(assigned_agent_id=AGENT_ID, customer_id=CUSTOMER_ID, internal=True)

    parsed = events.parse_envelope(events.serialize(envelope))

    assert parsed == envelope
    assert isinstance(parsed, events.TicketEventEnvelope)


def test_a_notification_envelope_parses_as_its_own_kind() -> None:
    """The discriminator `parse_envelope` switches on.

    The two envelope shapes have overlapping field names and no shared base, so the
    decision has to be made before either model is applied. Getting it wrong would not raise
    — `TicketEventEnvelope` would parse a notification body and fail on `ticket_id` being
    absent, which reads as a malformed message rather than a dispatch bug.
    """
    parsed = events.parse_envelope(events.serialize(notification_envelope()))

    assert isinstance(parsed, events.NotificationEnvelope)
    assert parsed.title == "Ticket #42 was assigned to you"


@pytest.mark.parametrize(
    "payload",
    [
        # Not JSON at all.
        "not json",
        # JSON, but not an envelope.
        json.dumps({"hello": "world"}),
        # An envelope-shaped body with a field the vocabulary does not have. Pydantic
        # ignores extras by default, so what makes this unreadable is the missing
        # `organization_id` rather than the surplus key — which is the point: the model is
        # what decides, not a hand-written field check.
        json.dumps({"type": "ticket.created", "ticket_id": str(TICKET)}),
        # A ticket event whose `type` is not a real-time event at all.
        json.dumps(
            {
                "type": "sla_warning",
                "organization_id": str(ORG),
                "ticket_id": str(TICKET),
                "ticket_number": 1,
            }
        ),
    ],
)
def test_an_unreadable_message_raises_rather_than_being_guessed_at(payload: str) -> None:
    """`parse_envelope` raises, and the subscriber decides what that means.

    Deciding here would put a policy about loop resilience inside a decoder. The subscriber
    logs the message and keeps listening, which is the answer for a single bad message on a
    shared channel — and the reason this function is allowed to be unforgiving.
    """
    with pytest.raises((ValidationError, ValueError)):
        events.parse_envelope(payload)


# ---------------------------------------------------------------------------
# The boundary — row scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_row_scope_decides_who_sees_a_ticket_event(role: UserRole) -> None:
    """One case per role, built from the map that governs the HTTP reads.

    `TICKET_SCOPE_BY_ROLE` is read rather than restated, so this asserts that the socket
    *consults* the one table rather than that a second copy of it happens to agree today.
    An admin or manager reaches every ticket in the organization; an agent reaches the ones
    assigned to them and nobody else's; a customer reaches their own.

    Each context is built to be the one entitled to see this envelope — the agent is the
    assignee, the customer is the ticket's customer — so a role that came back `False` here
    would be one the socket had silently blinded.
    """
    scope = TICKET_SCOPE_BY_ROLE[role]
    envelope = ticket_envelope(assigned_agent_id=AGENT_ID, customer_id=CUSTOMER_ID)
    viewer = context(
        role,
        user_id=AGENT_ID if scope is RowScope.ASSIGNED else USER_ID,
        customer_id=CUSTOMER_ID if scope is RowScope.OWN else None,
    )

    assert events.ticket_event_visible_to(envelope, viewer) is True


def test_an_agent_does_not_see_a_colleagues_assignment() -> None:
    """The agent scope, where the map alone is not enough.

    An agent's `RowScope.ASSIGNED` says "tickets assigned to me", so the comparison has to
    be against the connection's *user id* and not against its role. A predicate that
    returned `True` for every agent would pass the test above and hand every agent the
    organization's whole firehose.
    """
    envelope = ticket_envelope(assigned_agent_id=AGENT_ID)

    colleague = context(UserRole.AGENT, user_id=uuid.uuid4())

    assert events.ticket_event_visible_to(envelope, colleague) is False
    assert events.ticket_event_visible_to(envelope, context(UserRole.AGENT, user_id=AGENT_ID))


def test_an_unassigned_ticket_reaches_no_agent() -> None:
    """`None` on both sides is not a match.

    This is the case a naive `envelope.assigned_agent_id == context.user_id` gets wrong
    only if it is written as `is not None` on the wrong side — and it is the case that
    matters most, because the assignment being cleared is precisely when a stale
    subscription would leak: a reopened ticket is unassigned, and an agent who used to hold
    it must stop hearing about it.
    """
    envelope = ticket_envelope(assigned_agent_id=None)

    assert events.ticket_event_visible_to(envelope, context(UserRole.AGENT)) is False


def test_a_portal_account_with_no_customer_reaches_nothing() -> None:
    """ADR-015's reading of a null `customer_id`, on the socket.

    A portal user whose link to a `Customer` row was never made (or was removed) must reach
    no rows. The other reading — "no customer means no filter" — would hand them every
    ticket in the organization, which is the failure ADR-015 exists to record.
    """
    envelope = ticket_envelope(customer_id=CUSTOMER_ID)

    assert events.ticket_event_visible_to(envelope, context(UserRole.CUSTOMER)) is False


def test_an_unknown_role_falls_closed() -> None:
    """A role the map does not name resolves to the *narrowest* scope, not the widest.

    `row_scope_for` defaults to `RowScope.OWN`, and the predicate's last branch is the `OWN`
    comparison — so a role added to `UserRole` without a `TICKET_SCOPE_BY_ROLE` entry sees
    only tickets whose `customer_id` equals its own, which for a staff role is no tickets at
    all. Asserted against `row_scope_for` with an empty map because the predicate resolves
    its map internally, which is the right design: the fallback is the property, and the
    predicate's use of it is asserted by
    `test_a_portal_account_with_no_customer_reaches_nothing`.

    The other default — "unknown means unrestricted" — is the failure this guards, and it is
    the reading a convenience change would pick.
    """
    assert row_scope_for(UserRole.ADMIN, {}) is RowScope.OWN


# ---------------------------------------------------------------------------
# The boundary — organizations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_a_different_organization_is_refused_for_every_role(role: UserRole) -> None:
    """No role is exempt, and the check runs before the scope check.

    Parametrized over every role rather than asserted once, because "the admin scope
    returns `True`" is exactly the kind of branch that gets written first and returns
    before the tenant comparison happens. An event from another organization must reach
    nobody, whatever they are.
    """
    envelope = ticket_envelope(
        organization_id=OTHER_ORG, assigned_agent_id=AGENT_ID, customer_id=CUSTOMER_ID
    )

    assert (
        events.ticket_event_visible_to(
            envelope,
            context(role, user_id=AGENT_ID, customer_id=CUSTOMER_ID),
        )
        is False
    )


# ---------------------------------------------------------------------------
# The boundary — internal content
# ---------------------------------------------------------------------------


def test_an_internal_note_does_not_reach_the_customer_it_is_about() -> None:
    """**The leak the first design of this module would have shipped.**

    Row scope alone says a customer may see their own ticket, and they may — but an
    internal note on it is staff-only, and `MESSAGE_READ_INTERNAL` is the capability that
    already answers that on the HTTP side, applied in the service because a route cannot
    express "this field, for this audience".

    So the same customer who *should* receive `ticket.message_added` for their own ticket
    must not receive `ticket.note_added`, and the only difference between the two envelopes
    is this flag. Asserted both ways, because a predicate that refused every internal event
    to everyone would pass the first assertion and silently blind the agents the notes are
    written for.
    """
    note = ticket_envelope(
        type=events.RealtimeEventType.TICKET_NOTE_ADDED,
        assigned_agent_id=AGENT_ID,
        customer_id=CUSTOMER_ID,
        internal=True,
    )
    portal_user = context(UserRole.CUSTOMER, customer_id=CUSTOMER_ID)

    assert events.ticket_event_visible_to(note, portal_user) is False

    for role in (UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT):
        assert events.ticket_event_visible_to(
            note,
            # The agent is the assignee, so this is entitled to the ticket by row scope and
            # the only question left is whether the internal flag shuts them out. It must not
            # — an internal note is written *for* the desk.
            context(role, user_id=AGENT_ID, customer_id=None),
        ), f"{role} holds MESSAGE_READ_INTERNAL and must see the note"


def test_the_internal_flag_is_the_only_difference_that_matters() -> None:
    """The same ticket, the same customer, two envelopes, two answers.

    Stated as a pair so the test cannot pass because of something else about the envelope —
    a different ticket, a different organization, or a `type` the predicate ignores. The
    only field that changed is `internal`, so the only thing that can explain the two
    different answers is the rule under test.
    """
    portal_user = context(UserRole.CUSTOMER, customer_id=CUSTOMER_ID)
    public = ticket_envelope(
        type=events.RealtimeEventType.TICKET_MESSAGE_ADDED, customer_id=CUSTOMER_ID
    )
    internal = ticket_envelope(
        type=events.RealtimeEventType.TICKET_MESSAGE_ADDED, customer_id=CUSTOMER_ID, internal=True
    )

    assert events.ticket_event_visible_to(public, portal_user) is True
    assert events.ticket_event_visible_to(internal, portal_user) is False


# ---------------------------------------------------------------------------
# The boundary — notifications
# ---------------------------------------------------------------------------


def test_a_notification_reaches_its_addressee_and_nobody_else() -> None:
    """Addressee equality, which is the whole rule.

    Not re-derived from the role or from the ticket: who a notification was for was decided
    when it was written, and it is already durable in the row. Recomputing it here would be
    a second implementation of the notification policy, agreeing with the first until one of
    them changed.
    """
    envelope = notification_envelope(user_id=USER_ID)

    assert events.notification_visible_to(envelope, context(UserRole.AGENT, user_id=USER_ID))
    assert not events.notification_visible_to(
        envelope, context(UserRole.AGENT, user_id=uuid.uuid4())
    )


def test_a_notification_from_another_organization_is_refused() -> None:
    """Both halves are checked, and the organization half is not redundant with the user id.

    A user id is unique across the fleet, so an envelope naming this user *and* another
    organization should be impossible. Checked anyway: a boundary that only holds because
    of something else is a boundary that moves the day that something else changes.
    """
    envelope = notification_envelope(organization_id=OTHER_ORG, user_id=USER_ID)
    viewer = context(UserRole.AGENT, user_id=USER_ID)

    assert events.notification_visible_to(envelope, viewer) is False


def test_a_manager_does_not_receive_a_colleagues_notification() -> None:
    """The organization-wide row scope does not apply to notifications.

    This is the distinction between the two predicates, and it is easy to lose: an admin
    reaches every ticket, so it is tempting to conclude they reach every notification about
    one. They do not — a notification is addressed to a person, and the ticket scope says
    nothing about whose toast it is.
    """
    envelope = notification_envelope(user_id=USER_ID)

    assert (
        events.notification_visible_to(envelope, context(UserRole.ADMIN, user_id=uuid.uuid4()))
        is False
    )


def test_the_dispatcher_picks_the_right_predicate_for_each_kind() -> None:
    """`envelope_visible_to` is the one entry point the manager uses.

    A manager holding an event and a notification must be told different things about them,
    and the only difference between the two calls is the object. If the dispatch were ever
    collapsed to one predicate, one of these two assertions would fail — which is the point
    of asserting both through the same function.
    """
    manager = context(UserRole.MANAGER, user_id=uuid.uuid4())

    colleague_ticket_event = ticket_envelope(assigned_agent_id=AGENT_ID)
    colleague_notification = notification_envelope(user_id=USER_ID)

    assert events.envelope_visible_to(colleague_ticket_event, manager) is True
    assert events.envelope_visible_to(colleague_notification, manager) is False
