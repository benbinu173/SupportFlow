"""Notifications — the policy that decides who is told, and the reads that show it.

Spec §26 lists the in-app notifications the product owes a user: ticket assigned,
ticket reassigned, new customer reply, manager mention, SLA warning, AI analysis
completed, and ticket resolved. Four of the seven have producers today; the other three
are recorded below with the phase that brings them, so "not implemented yet" is a
statement in the code rather than an omission a reader has to notice.

Two halves, and the split between them is the design
----------------------------------------------------

**Staging is part of the caller's transaction.** `notify_for_event` never commits,
exactly like `ticket_service.record_event` and `audit_service.record_for`. The
notification row lands in the same unit of work as the change it describes, so a
rolled-back reassignment cannot leave behind a notification saying it happened. That
is not a nicety: a notification is a claim about the world, and a claim written by a
transaction that then failed is simply false.

**Delivery is not.** `enqueue_delivery` runs *after* the caller's commit and does the
one thing that must not be inside it — talking to the broker. The order is not a
preference, it is a requirement: the task it queues reads the notification row by id,
and a task that started before that row was committed would find nothing and quietly
do nothing. `tests/api/test_notifications.py` asserts the order rather than trusting it.

Email is the delivery mechanism, not the record. The row is the source of truth, which
is why a broker outage degrades the *email* and never the notification — see
`enqueue_delivery`.

The events that notify nobody, and the ones that notify on a schedule
--------------------------------------------------------------------
§26 names seven triggers. The ticket timeline has twelve event types, and the ten that are
not "handled here" split into three groups that mean different things:

**Named by §26, produced on a schedule rather than by a request** — `SLA_WARNING` and
`SLA_BREACHED`. `app/workers/sla_tasks.py` writes these from a beat task; no inbound
request can produce one. They are the reason this module has a second entry point.

**Named by §26, with no producer yet** — `AI_ANALYSIS_COMPLETED`. *Deferred*, not
declined, and it carries the phase that brings it.

**Not named by §26 at all** — `CREATED`, `UNASSIGNED`, `PRIORITY_CHANGED`,
`INTERNAL_NOTE_ADDED`, `ATTACHMENT_ADDED`, and `REOPENED`. Each is a deliberate no: a
creator knows they created it, an unassignment notifies nobody because the agent losing
the ticket is not the one who needs to act, a priority change is visible on the ticket, an
internal note is for the desk rather than the customer, and a reopened ticket goes back on
the queue where assignment notifies whoever picks it up. None of that is in §26, and
inventing triggers the specification does not ask for is how a notification centre becomes
something users mute.

Keeping the groups apart matters, because "we chose not to", "we have not yet", and "the
scheduler will" are three different answers to a reader asking why nothing was sent.
`tests/unit/test_notification_policy.py` walks every member of the enum and fails if one is
in none of them or in two — which is how the division below was found to be wrong the first
time it was written, and how Phase Q found the two entries it was holding open.

**Phase Q is the answer to the two comments this module used to carry.** `SILENT_EVENT_TYPES`
held `SLA_BREACHED` with a note saying Phase Q "will know whether the breach is a second
alert or a correction of the first", and `DEFERRED_EVENT_TYPES` held `SLA_WARNING` naming
this phase as its producer. Both now have one, and the decision on the breach is recorded
on `notify_sla_alert` below.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ErrorCode, NotFoundError
from app.core.tenancy import TenantContext
from app.models.enums import NotificationType, SenderType, TicketEventType, TicketStatus
from app.models.message import Message
from app.models.notification import Notification
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.repositories.notification_repository import NotificationRepository
from app.repositories.user_repository import UserRepository
from app.schemas.sla import SLATimer
from app.services.sla_service import SLAAlert

logger = structlog.get_logger(__name__)

# Every event type that notifies nobody, and why. Membership in one of these three sets is
# the only alternative to being handled, so a new `TicketEventType` cannot arrive without
# a decision — `tests/unit/test_notification_policy.py` asserts the four sets partition
# the enum.

# §26 does not name these, so nothing is owed. See the module docstring for each.
SILENT_EVENT_TYPES: frozenset[TicketEventType] = frozenset(
    {
        TicketEventType.CREATED,
        TicketEventType.UNASSIGNED,
        TicketEventType.PRIORITY_CHANGED,
        TicketEventType.INTERNAL_NOTE_ADDED,
        TicketEventType.ATTACHMENT_ADDED,
        TicketEventType.REOPENED,
    }
)

# §26 names these and nobody produces them yet. Deferred rather than declined, and
# separated from the set above so "we chose not to notify" and "we cannot yet" stay
# distinguishable when someone asks why an event was quiet.
DEFERRED_EVENT_TYPES: frozenset[TicketEventType] = frozenset(
    {
        # Phases T-W, which build the analysis this would announce. Note it is the
        # *completion* §26 asks about: §41 requires AI output be reviewed by a person
        # before a customer sees it, so announcing that it is ready is an alert to the
        # agent, not to the customer.
        TicketEventType.AI_ANALYSIS_COMPLETED,
    }
)

# Produced by `app/workers/sla_tasks.py` on a schedule, never by an inbound request. Kept
# separate from `_HANDLED_EVENT_TYPES` because `notify_for_event` is the *request* policy
# and an SLA alert has no request — the division says which code path sends it, not whether
# it is sent.
#
# The two members were the last entries of the two sets above, each with a comment naming
# this phase. `SLA_BREACHED` sat in `SILENT_EVENT_TYPES` because §26 names the warning and
# not the breach, and the question was whether a breach is a second alert or a correction
# of the first. It is a second alert: see `notify_sla_alert`.
SCHEDULED_EVENT_TYPES: frozenset[TicketEventType] = frozenset(
    {TicketEventType.SLA_WARNING, TicketEventType.SLA_BREACHED}
)


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


async def notify_for_event(
    session: AsyncSession,
    context: TenantContext,
    ticket: Ticket,
    event: TicketEvent,
    *,
    message: Message | None = None,
) -> list[Notification]:
    """Stage the notifications an event warrants. **Never commits.**

    Returns a list, and more than one element is reachable today: a resolution is
    addressed to every portal login the ticket's customer has, and a customer record may
    have several (see `_portal_users_for`). The list return type was chosen for that
    before the case existed — the alternative, `Notification | None`, would have had to
    change shape the first time a trigger addressed two people.

    **Nothing here parses a timeline value back into structured data.** `TicketEvent`'s
    `from_value`/`to_value` are documented as display strings, and reading a uuid out of
    a `String(100)` column would break the day somebody renders an agent's name there
    instead. So the recipient and the meaning both come from typed columns: the assignee
    is `ticket.assigned_agent_id`, which `assign_ticket` has already set by the time
    this runs, and "was it resolved" is `ticket.status`, which `_transition` has already
    set. The one thing read off the event is whether `from_value` is null, which is a
    fact about the ticket's history — "was there an agent before this" — and not a value
    being decoded.

    `message` is required for exactly one event type. `MESSAGE_ADDED` does not say who
    wrote the message, and the distinction that matters is `message.sender_type`, not
    who the caller is: an agent's reply and an `AI_DRAFT` are both staff-side, and §41
    requires the draft never be mistaken for the customer having written in.
    """
    if event.event_type in SCHEDULED_EVENT_TYPES:
        # Unreachable from any route today — `app/workers/sla_tasks.py` is the only writer
        # of these two event types, and it calls `notify_sla_alert` rather than this. The
        # guard is here anyway because "unreachable" is a claim about today, and if a
        # request ever did produce one, staging zero rows is right and alerting twice is
        # not.
        return []

    if event.event_type in SILENT_EVENT_TYPES or event.event_type in DEFERRED_EVENT_TYPES:
        return []

    repository = NotificationRepository(session, context)
    notifications: list[Notification] = []

    for recipient_id in await _recipients_for(session, context, ticket, event, message=message):
        # Filtered per recipient, not per event. A manager assigning a ticket to
        # themselves, or an agent picking up a ticket that was already theirs, does not
        # need an alert about their own click — and a notification centre whose first few
        # entries are the user's own actions is one they stop opening.
        if recipient_id == context.user_id:
            continue

        notification = Notification(
            organization_id=context.organization_id,
            user_id=recipient_id,
            notification_type=_notification_type_for(ticket, event),
            title=_title_for(ticket, event),
            # The ticket's number and subject, and nothing else. Both are bounded by their
            # columns — `number` is an integer and `subject` is `String(500)` — so the body
            # cannot exceed this column's 1000 characters, which is why there is no
            # truncation here. A copy of the message body would not have that property.
            body=f"#{ticket.number} - {ticket.subject}",
            ticket_id=ticket.id,
        )
        repository.add(notification)
        notifications.append(notification)

        logger.info(
            "notification_staged",
            notification_type=str(notification.notification_type),
            organization_id=str(context.organization_id),
            # Ids, never the recipient's email address: the log is a debugging aid, and an
            # address in it is a piece of personal data that ends up wherever logs end up.
            recipient_id=str(recipient_id),
            ticket_id=str(ticket.id),
            actor_id=str(context.user_id),
        )

    return notifications


async def _recipients_for(
    session: AsyncSession,
    context: TenantContext,
    ticket: Ticket,
    event: TicketEvent,
    *,
    message: Message | None,
) -> list[uuid.UUID]:
    """Everyone the event is addressed to. Empty when it is addressed to nobody.

    Only §26's three produced triggers reach this: everything else returned early in
    `notify_for_event`. The fourth produced trigger, "manager mention", is deferred for a
    reason of its own — the phrase appears exactly once in the whole specification, in
    §26's bullet list, with no syntax, no resolution rule, and no UI anywhere else, so
    there is nothing to implement and nothing to guess. `NotificationType.MENTION` stays
    in the enum, unreachable, awaiting a definition (ADR-023).
    """
    if event.event_type is TicketEventType.ASSIGNED:
        # The new agent, from the typed column. `assign_ticket` sets it before calling
        # here, so this is the post-change value whether the ticket was assigned or
        # reassigned.
        return _at_most_one(ticket.assigned_agent_id)

    if event.event_type is TicketEventType.MESSAGE_ADDED:
        if message is None or message.sender_type is not SenderType.CUSTOMER:
            return []
        # The agent working the ticket. `None` when nobody is assigned — a customer
        # reply on an unassigned ticket is the queue's problem, and there is no one
        # person to alert about it.
        return _at_most_one(ticket.assigned_agent_id)

    if event.event_type is TicketEventType.STATUS_CHANGED:
        if ticket.status is not TicketStatus.RESOLVED:
            return []
        # The customer, via their portal logins. This is the one case where the natural
        # recipient is the external party, and `notifications.user_id` is `NOT NULL` —
        # so a customer with no portal account has nobody to notify and no row is
        # written. Recorded as a known limitation in the README rather than solved by
        # inventing a second recipient model §26 does not describe (ADR-023).
        return await _portal_users_for(session, context, ticket.customer_id)

    # Unreachable: the three event types handled above are the only ones that survive
    # `notify_for_event`'s early return, and the unit test asserts that the two sets and
    # these three partition the enum. Returning `[]` rather than raising keeps a
    # notification failure from breaking the request that caused it, which is the right
    # trade for an alert.
    return []


def _at_most_one(user_id: uuid.UUID | None) -> list[uuid.UUID]:
    """Zero or one recipient, in the shape every branch above returns.

    The assignment and reply triggers address exactly one person, so this exists only to
    keep their branches from being the odd ones out — a `None` check at each call site
    that all three return types agree is worth more than the two lines it saves.
    """
    return [] if user_id is None else [user_id]


async def _portal_users_for(
    session: AsyncSession, context: TenantContext, customer_id: uuid.UUID
) -> list[uuid.UUID]:
    """Every active portal login linked to a customer.

    Plural, because the link is not unique — see `UserRepository.list_by_customer_id`
    for why, and for the 500 that assuming otherwise produced. Everyone who can sign in
    as this customer is told their ticket was resolved; picking one of them would be
    choosing which of the customer's addresses goes without.

    Inactive accounts are dropped. Their owner cannot sign in to read the notification
    and will not receive the email either, so keeping them would produce an alert
    addressed to nobody — and one that would never be marked read.
    """
    users = await UserRepository(session, context).list_by_customer_id(customer_id)
    return [user.id for user in users if user.is_active]


def _notification_type_for(ticket: Ticket, event: TicketEvent) -> NotificationType:
    """Which of §26's triggers this is.

    Assigned and reassigned are separate members and separate sentences to the user: a
    ticket arriving for the first time is new work, while one moving from a colleague is
    a handover with a history. `from_value` is null exactly when there was no previous
    agent, which is the whole difference — see `notify_for_event` on why this is the one
    fact read off the timeline row.

    The last branch is a `STATUS_CHANGED` that reached here, and only a resolution can:
    `_recipient_for` returns `None` for every other status, because §26 lists "ticket
    resolved" and nothing else that a status change can mean. The three functions below
    are therefore a matched set, and a fourth handled event type would have to add a
    branch to each.
    """
    if event.event_type is TicketEventType.ASSIGNED:
        return (
            NotificationType.TICKET_REASSIGNED
            if event.from_value is not None
            else NotificationType.TICKET_ASSIGNED
        )
    if event.event_type is TicketEventType.MESSAGE_ADDED:
        return NotificationType.NEW_CUSTOMER_REPLY
    return NotificationType.TICKET_RESOLVED


def _title_for(ticket: Ticket, event: TicketEvent) -> str:
    """ "Ticket assigned to you", in the voice the reader needs.

    Written from the recipient's point of view, not the actor's: the person reading
    this did not do the thing, they are being told about it.
    """
    if event.event_type is TicketEventType.ASSIGNED:
        return (
            "Ticket reassigned to you" if event.from_value is not None else "Ticket assigned to you"
        )
    if event.event_type is TicketEventType.MESSAGE_ADDED:
        return "New reply from the customer"
    return "Your ticket has been resolved"


def enqueue_delivery(notifications: Sequence[Notification]) -> int:
    """Queue an email for each notification. **Call this after the commit.**

    Synchronous, because publishing to a broker is — and because this runs in a request
    that has already committed, where awaiting anything further buys nothing.

    The task is given an id and nothing else. That is deliberate: copying the message
    into the broker would create a second copy of the notification's content, and the
    two would disagree the first time one of them was edited. It is also why the commit
    has to come first — the task's first act is to read that row.

    **A broker that is down does not fail the request.** The notification is already
    committed and the user will see it; only the email is lost, and losing an email is
    strictly better than returning a 500 for an action that succeeded. The exception is
    logged by *type* and without a traceback: a connection error's message embeds the
    broker URL, and in production that URL carries a password (§4 — no secrets in logs).

    Returns how many were queued, so a caller or a test can tell "nothing to send" from
    "queued and forgotten".
    """
    if not notifications:
        return 0

    # Imported here rather than at module scope so the API process does not import the
    # task module on every startup. Nothing above this line needs Celery to exist, and
    # the dependency stays visible at the one place it is used.
    from app.workers.email_tasks import send_notification_email

    queued = 0
    for notification in notifications:
        try:
            send_notification_email.delay(str(notification.id))
        except Exception as exc:
            logger.warning(
                "notification_delivery_not_queued",
                notification_id=str(notification.id),
                organization_id=str(notification.organization_id),
                error_type=type(exc).__name__,
            )
            continue
        queued += 1
    return queued


# The two scheduled event types and the notification each produces. A mapping rather than
# a derived name, so a change to one enum cannot silently change what the other means —
# and the same shape as `_notification_type_for`, which is the request path's version of
# this decision.
_SCHEDULED_NOTIFICATION_TYPES: dict[TicketEventType, NotificationType] = {
    TicketEventType.SLA_WARNING: NotificationType.SLA_WARNING,
    TicketEventType.SLA_BREACHED: NotificationType.SLA_BREACHED,
}

# The two timers, in the words a person uses for them. Kept here rather than derived from
# the enum member's name so the sentence reads as English and stays under `title`'s 200
# characters without anyone having to count.
_TIMER_LABELS: dict[SLATimer, str] = {
    SLATimer.RESPONSE: "first response",
    SLATimer.RESOLUTION: "resolution",
}


async def notify_sla_alert(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    ticket: Ticket,
    alert: SLAAlert,
) -> list[Notification]:
    """Stage the notifications for one alert the sweep found due. **Never commits.**

    The second entry point into staging, and the only one that runs without a request. Why
    it is a function here rather than a branch of `notify_for_event`: that one is addressed
    from a `TicketEvent` a caller just produced, filters the actor out of their own alert,
    and assumes a `TenantContext`. This has no event to read — the sweep is *producing* it —
    no actor to filter, and no context, since `app/workers/sla_tasks.py` runs with no
    authenticated identity. Passing a fabricated `TenantContext` to reuse the other
    function would mean inventing a user id that is not a user (ADR-009, spec §4).

    `alert` comes from `sla_service.due_alerts`, which owns the judgement of *what* is due;
    this function owns only *who* hears about it. The split is why the sweep's rule can be
    unit-tested without a database, and why the two enums meet in exactly one place.

    **A breach is a second alert, not a correction of the first.** §26 names only the
    warning; the breach is this phase's addition, because "you have 20 minutes" and "you are
    40 minutes late" were both true when they were sent and are different facts about the
    world. Nothing is retracted when the second goes out.

    **The assignee and every active manager**, which is §27's "agents/managers when
    appropriate" read as a fan-out. The manager owns the queue, so a deadline on a ticket
    nobody picked up is their business rather than nobody's — which is why an unassigned
    ticket still produces alerts, addressed to the managers alone. A system alert with no
    recipient is the exact failure the feature exists to prevent.

    The assignee comes off the typed `ticket.assigned_agent_id` and is not re-checked for
    activity, unlike the managers. There is nothing to correct if it turns out to be a
    deactivated account: the managers are on the alert either way, and a ticket still
    assigned to someone who has left is one they need to know about.

    `due_at` is carried into the title *and* stored on the timeline entry by the caller,
    both deliberately. A policy edited next month must not rewrite what the alert said
    happened, and the recipient's question is "what was I racing", which only the deadline
    at the time answers.

    Never commits. `app/workers/sla_tasks.py` commits once per organization, after staging
    every alert for every ticket in it.
    """
    from app.repositories import sla_repository

    # The one place the timeline's vocabulary and the notification's meet. Above the
    # recipient loop because it is a property of the alert, not of who receives it.
    notification_type = _SCHEDULED_NOTIFICATION_TYPES[alert.event_type]

    # Order matters and so does the dedupe: a manager working a ticket they are also
    # assigned to is one person owed one alert, not two, and `dict.fromkeys` keeps the
    # assignee first — the person with the most direct claim to it.
    recipient_ids = dict.fromkeys(
        [
            *([ticket.assigned_agent_id] if ticket.assigned_agent_id is not None else []),
            *await sla_repository.find_manager_ids(session, organization_id),
        ]
    )

    notifications: list[Notification] = []
    for recipient_id in recipient_ids:
        notification = Notification(
            organization_id=organization_id,
            user_id=recipient_id,
            notification_type=notification_type,
            title=_sla_title(notification_type, alert.timer, alert.due_at),
            # The same two bounded columns as every other notification, so the length
            # argument in `notify_for_event` holds here unchanged: `number` is an integer
            # and `subject` is `String(500)`.
            body=f"#{ticket.number} - {ticket.subject}",
            ticket_id=ticket.id,
        )
        # `session.add` rather than a repository, for the reason `audit_service.record`
        # gives: `NotificationRepository` is tenant-scoped, and constructing one from a
        # context that does not exist would add a constructor argument and nothing else.
        session.add(notification)
        notifications.append(notification)

        logger.info(
            "notification_staged",
            notification_type=str(notification_type),
            organization_id=str(organization_id),
            recipient_id=str(recipient_id),
            ticket_id=str(ticket.id),
            timer=str(alert.timer),
        )

    return notifications


def _sla_title(notification_type: NotificationType, timer: SLATimer, due_at: datetime) -> str:
    """ "SLA warning: first response is due 2026-09-16 14:32 UTC", in the reader's voice.

    The deadline is in the title rather than the body because it is the one fact the
    recipient has to act on, and it is rendered in UTC with the zone spelled out — an alert
    that says "14:32" to someone in another timezone is worse than one that says nothing.
    `astimezone` rather than trusting the value's own zone, since a `timestamptz` column
    is only UTC by convention and the render should not depend on that holding.
    """
    label = _TIMER_LABELS[timer]
    deadline = due_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if notification_type is NotificationType.SLA_BREACHED:
        return f"SLA breach: {label} was due {deadline}"
    return f"SLA warning: {label} is due {deadline}"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_for_user(
    session: AsyncSession,
    context: TenantContext,
    *,
    unread_only: bool,
    limit: int,
    offset: int = 0,
) -> list[Notification]:
    """A page of the caller's own notifications, newest first.

    "The caller's own" and not "the organization's": `NOTIFICATION_LIST` is held by all
    four roles, and the row scope that would otherwise come from §3's matrix does not
    apply — a notification is addressed to one person, so there is nothing for a role to
    widen. The repository applies that predicate; this function cannot remove it.
    """
    return list(
        await NotificationRepository(session, context).list_for_user(
            unread_only=unread_only, limit=limit, offset=offset
        )
    )


async def unread_count(session: AsyncSession, context: TenantContext) -> int:
    """How many of the caller's notifications are unread — the badge number."""
    return await NotificationRepository(session, context).count_unread()


async def mark_read(
    session: AsyncSession, context: TenantContext, notification_id: uuid.UUID
) -> Notification:
    """Mark one notification as read, or 404.

    A notification belonging to a colleague produces the same 404 as one that does not
    exist, because the lookup in the repository cannot tell the two apart — and neither
    should the caller be able to (ADR-009).

    Re-marking an already-read notification returns it unchanged rather than moving its
    timestamp. A client that retries the request should not be able to make the read
    time drift, and "when did they read it" is worth being able to answer.
    """
    repository = NotificationRepository(session, context)
    notification = await repository.get_for_user(notification_id)
    if notification is None:
        raise NotFoundError(ErrorCode.NOTIFICATION_NOT_FOUND)

    if notification.read_at is None:
        repository.mark_read(notification)
        await session.commit()

        logger.info(
            "notification_read",
            notification_id=str(notification.id),
            organization_id=str(context.organization_id),
            user_id=str(context.user_id),
        )
    return notification


async def mark_all_read(session: AsyncSession, context: TenantContext) -> int:
    """Mark everything unread as read. Returns how many rows changed.

    One `UPDATE` and one count, with no per-row logging: this is a bulk dismissal of
    notifications the user has decided not to read individually, and a log line per row
    would be a burst of writes behind a button whose whole purpose is to avoid that.
    """
    changed = await NotificationRepository(session, context).mark_all_read()
    await session.commit()

    logger.info(
        "notifications_read_all",
        organization_id=str(context.organization_id),
        user_id=str(context.user_id),
        count=changed,
    )
    return changed
