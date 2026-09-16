"""Who an event notifies — the policy, asserted event type by event type.

Spec §26 names seven triggers; the ticket timeline has twelve event types. The gap is the
thing worth testing, because a gap is invisible: a notification that is never sent looks
exactly like a notification that had nobody to send to. So the central test here walks
every member of `TicketEventType` and fails if one is neither handled by the policy nor
listed as deliberately silent — the same mechanical-sweep shape
`tests/security/test_route_protection.py` uses for routes and capabilities.

**Why this file needs no database.** The policy is a mapping from an event to a
recipient, and three of the four producing cases resolve the recipient from a column that
is already loaded. The one case that has to look something up — "the customer's portal
login", for a resolution — is exercised end to end in `tests/api/test_notifications.py`,
where a real ticket can be resolved by a real request. Rather than reach for a session
here, the tests pass a sentinel that raises on any attribute access, which turns "this
path does not query" from an assumption into an assertion.
"""

import uuid
from typing import Any, cast

import pytest

from app.core.tenancy import TenantContext
from app.models.enums import NotificationType, SenderType, TicketEventType, TicketStatus
from app.models.message import Message
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.services import notification_service
from app.services.notification_service import DEFERRED_EVENT_TYPES, SILENT_EVENT_TYPES

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


class _NoDatabase:
    """A stand-in for the session that fails if anything *reads* through it.

    Stronger than passing `None`: an unguarded query would raise `AttributeError`, which
    is also what a typo in the test would produce, and the two are indistinguishable in a
    failure message. This says what actually went wrong.

    `add` is allowed, because staging the row is the one thing this path is supposed to do
    with a session — it is what makes the notification part of the caller's transaction.
    Everything else is a lookup, and a lookup here would mean the recipient was resolved
    from the database rather than from a column already loaded.
    """

    def add(self, entity: object) -> object:
        return entity

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"this notification path issued a database query ({name!r}) - it should "
            "resolve the recipient from a column already loaded"
        )


def _context(user_id: uuid.UUID, *, role: str = "agent") -> TenantContext:
    from app.models.enums import UserRole

    return TenantContext(
        user_id=user_id,
        organization_id=uuid.uuid4(),
        role=UserRole(role),
    )


def _ticket(
    *,
    assigned_agent_id: uuid.UUID | None = None,
    status: TicketStatus = TicketStatus.OPEN,
    number: int = 1042,
    subject: str = "The printer is on fire",
) -> Ticket:
    """An unsaved `Ticket`. Never flushed, so no session is needed to build one."""
    return Ticket(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        number=number,
        customer_id=uuid.uuid4(),
        assigned_agent_id=assigned_agent_id,
        subject=subject,
        description="It really is.",
        status=status,
    )


def _event(
    event_type: TicketEventType,
    *,
    from_value: str | None = None,
    to_value: str | None = None,
) -> TicketEvent:
    return TicketEvent(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        event_type=event_type,
        from_value=from_value,
        to_value=to_value,
        extra_data={},
    )


def _message(sender_type: SenderType) -> Message:
    return Message(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        sender_type=sender_type,
        sender_user_id=uuid.uuid4(),
        body="hello",
        is_internal=False,
    )


async def _notify(
    ticket: Ticket,
    event: TicketEvent,
    *,
    actor_id: uuid.UUID | None = None,
    message: Message | None = None,
    context: TenantContext | None = None,
) -> list[Any]:
    """Run the policy against a session that must not be used."""
    return await notification_service.notify_for_event(
        cast("Any", _NoDatabase()),
        context or _context(actor_id or uuid.uuid4()),
        ticket,
        event,
        message=message,
    )


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

# The event types the policy produces a notification for. Named here rather than derived,
# because deriving it from the same `if` chain under test would prove nothing.
_HANDLED_EVENT_TYPES = frozenset(
    {
        TicketEventType.ASSIGNED,
        TicketEventType.MESSAGE_ADDED,
        TicketEventType.STATUS_CHANGED,
    }
)


def test_every_event_type_is_notified_deliberately_silent_or_deferred() -> None:
    """No event type may be undecided.

    The failure this catches is a future phase adding a `TicketEventType` — a real one is
    coming for SLA breaches in Phase Q — and nobody asking whether it should notify
    anybody. An unhandled type falls through the policy's `if` chain and produces nothing,
    which is the correct behaviour by accident rather than by decision, and is
    indistinguishable from a bug.

    This test is not hypothetical: written against a two-way split it failed, and the
    three types it named — `SLA_WARNING`, `SLA_BREACHED`, and `AI_ANALYSIS_COMPLETED` —
    were exactly the ones the policy had left undecided.

    Three sets, not two, because "we chose not to notify" and "we cannot yet" are
    different answers. Equality in both directions, like the route allowlists: a type
    removed from the enum leaves a stale entry, which would excuse a new type reusing the
    name.
    """
    handled = _HANDLED_EVENT_TYPES
    silent = SILENT_EVENT_TYPES
    deferred = DEFERRED_EVENT_TYPES

    assert handled | silent | deferred == set(TicketEventType)
    assert not handled & silent
    assert not handled & deferred
    assert not silent & deferred


@pytest.mark.parametrize("event_type", sorted(SILENT_EVENT_TYPES))
async def test_a_silent_event_notifies_nobody(event_type: TicketEventType) -> None:
    """Each silent type produces nothing, and without reading the database.

    Parametrized over the set itself, so adding a member runs the new case automatically
    rather than requiring a matching test.
    """
    ticket = _ticket(assigned_agent_id=uuid.uuid4())

    assert await _notify(ticket, _event(event_type)) == []


@pytest.mark.parametrize("event_type", sorted(DEFERRED_EVENT_TYPES))
async def test_a_deferred_event_notifies_nobody_yet(event_type: TicketEventType) -> None:
    """The deferred types are quiet today, and this is the test that will need changing.

    When Phase Q or T-W gives one of them a producer, the type moves out of
    `DEFERRED_EVENT_TYPES` and into `_HANDLED_EVENT_TYPES` below — at which point this
    test stops running for it and a case has to be written for its recipient. That is the
    intended friction: the recipient of an SLA warning is a decision, not a default.
    """
    ticket = _ticket(assigned_agent_id=uuid.uuid4())

    assert await _notify(ticket, _event(event_type)) == []


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


async def test_assignment_notifies_the_new_agent() -> None:
    """§26's "ticket assigned". The recipient is the assignee, not the assigner."""
    agent_id = uuid.uuid4()
    manager_id = uuid.uuid4()
    manager = _context(manager_id, role="manager")
    ticket = _ticket(assigned_agent_id=agent_id, status=TicketStatus.ASSIGNED)

    staged = await _notify(
        ticket,
        _event(TicketEventType.ASSIGNED),
        context=manager,
    )

    assert len(staged) == 1
    notification = staged[0]
    assert notification.user_id == agent_id
    assert notification.notification_type is NotificationType.TICKET_ASSIGNED
    assert notification.ticket_id == ticket.id
    # From the *context*, not from the ticket. The two happen to agree here, which is why
    # the assertion names the caller's organization explicitly: a notification stamped
    # with a tenant off an entity would be a tenant chosen by whoever built the entity,
    # and §4 requires it come from the authenticated identity.
    assert notification.organization_id == manager.organization_id


async def test_reassignment_is_a_different_notification() -> None:
    """§26 lists assigned and reassigned separately, and `from_value` is the difference.

    A ticket arriving for the first time is new work; one moving from a colleague is a
    handover. Both the type and the title change, because the type is what a client
    filters on and the title is what the user reads.
    """
    ticket = _ticket(assigned_agent_id=uuid.uuid4(), status=TicketStatus.ASSIGNED)

    staged = await _notify(
        ticket,
        _event(TicketEventType.ASSIGNED, from_value=str(uuid.uuid4()), to_value=str(uuid.uuid4())),
    )

    assert len(staged) == 1
    assert staged[0].notification_type is NotificationType.TICKET_REASSIGNED
    assert "reassigned" in staged[0].title.lower()


async def test_assigning_a_ticket_to_yourself_notifies_nobody() -> None:
    """You do not need an alert about your own click.

    Not in §26, and the rule it follows is general: a notification centre whose first
    entries are the user's own actions is one they stop opening. It also makes the
    assignee and the actor the same person, which is the case this asserts.
    """
    manager_id = uuid.uuid4()
    ticket = _ticket(assigned_agent_id=manager_id, status=TicketStatus.ASSIGNED)

    staged = await _notify(
        ticket,
        _event(TicketEventType.ASSIGNED),
        actor_id=manager_id,
        context=_context(manager_id, role="manager"),
    )

    assert staged == []


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------


async def test_a_customer_reply_notifies_the_assigned_agent() -> None:
    """§26's "new customer reply". The recipient is whoever is working the ticket."""
    agent_id = uuid.uuid4()
    ticket = _ticket(assigned_agent_id=agent_id, status=TicketStatus.IN_PROGRESS)

    staged = await _notify(
        ticket,
        _event(TicketEventType.MESSAGE_ADDED),
        message=_message(SenderType.CUSTOMER),
    )

    assert len(staged) == 1
    assert staged[0].user_id == agent_id
    assert staged[0].notification_type is NotificationType.NEW_CUSTOMER_REPLY


@pytest.mark.parametrize("sender_type", [SenderType.AGENT, SenderType.SYSTEM])
async def test_a_non_customer_message_notifies_nobody(sender_type: SenderType) -> None:
    """An agent's own reply is not "a new customer reply", and neither is the system's.

    The distinction is read from the message row rather than from the caller's role.
    Reading the role would be nearly equivalent today and wrong the moment a message has
    an author that is not a user.
    """
    ticket = _ticket(assigned_agent_id=uuid.uuid4(), status=TicketStatus.IN_PROGRESS)

    assert (
        await _notify(ticket, _event(TicketEventType.MESSAGE_ADDED), message=_message(sender_type))
        == []
    )


async def test_an_ai_draft_is_not_a_customer_reply() -> None:
    """§41: an AI draft must never be mistaken for the customer having written in.

    `AI_DRAFT` is a member of `SenderType` for exactly this reason, and the notification
    policy is one of the places where confusing it would be user-visible — an agent would
    be alerted to a reply the customer never sent.
    """
    ticket = _ticket(assigned_agent_id=uuid.uuid4(), status=TicketStatus.IN_PROGRESS)

    assert (
        await _notify(
            ticket,
            _event(TicketEventType.MESSAGE_ADDED),
            message=_message(SenderType.AI_DRAFT),
        )
        == []
    )


async def test_a_customer_reply_on_an_unassigned_ticket_notifies_nobody() -> None:
    """Nobody is working it, so there is no one person to alert.

    The queue is the mechanism for that, and inventing a recipient — every agent, the
    managers — would be a fan-out §26 does not describe and a notification nobody owns.
    """
    ticket = _ticket(assigned_agent_id=None, status=TicketStatus.OPEN)

    staged = await _notify(
        ticket,
        _event(TicketEventType.MESSAGE_ADDED),
        message=_message(SenderType.CUSTOMER),
    )

    assert staged == []


async def test_a_message_event_without_a_message_notifies_nobody() -> None:
    """The one fact the timeline row does not carry has to be supplied.

    A caller that forgets it gets no notification rather than a wrong one, which is the
    fail-closed reading: an alert addressed to the wrong person is worse than no alert.
    """
    ticket = _ticket(assigned_agent_id=uuid.uuid4(), status=TicketStatus.IN_PROGRESS)

    assert await _notify(ticket, _event(TicketEventType.MESSAGE_ADDED)) == []


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


async def test_the_body_references_the_ticket_by_number_and_subject() -> None:
    """A notification is a sentence and a reference, and the reference is the ticket.

    Asserted because the body is composed from two columns, and a notification whose body
    says only "something happened" is one the reader has to go and look up — which is the
    work the notification existed to save them.
    """
    agent_id = uuid.uuid4()
    ticket = _ticket(
        assigned_agent_id=agent_id,
        number=1042,
        subject="The printer is on fire",
        status=TicketStatus.ASSIGNED,
    )

    staged = await _notify(ticket, _event(TicketEventType.ASSIGNED))

    assert staged[0].body == "#1042 - The printer is on fire"


async def test_the_body_fits_the_column_for_the_longest_subject_allowed() -> None:
    """500 characters is the subject's ceiling; 1000 is the body's.

    Both are database widths, so an overlong body is an integrity error at commit rather
    than a truncated string — which would surface as a 500 on an assignment. The
    arithmetic is checked rather than assumed, because it depends on a column width in a
    different module that a migration could change.
    """
    agent_id = uuid.uuid4()
    ticket = _ticket(
        assigned_agent_id=agent_id,
        number=999_999,
        subject="x" * 500,
        status=TicketStatus.ASSIGNED,
    )

    staged = await _notify(ticket, _event(TicketEventType.ASSIGNED))

    assert len(staged[0].body) <= 1000
    assert len(staged[0].title) <= 200
