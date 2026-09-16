"""The SLA clock — one pure function, and everything that feeds it.

**The whole design turns on `resolve_position` being pure.** It takes a `Ticket`, an
`SLAPolicy`, and a `now`, and returns where both of the ticket's timers stand. No session,
no context, no `await`, no clock of its own. Two callers that could otherwise disagree use
it: the API, when it renders a ticket's countdown to a person, and the sweep, when it
decides whether a deadline has passed. If those were two implementations they would agree
until the day one of them changed, and the symptom would be a dashboard showing a ticket on
track while the worker alerted about it — which is the failure this arrangement exists to
make impossible, and the same argument `app/models/enums.py` makes for `TICKET_TRANSITIONS`
having exactly one definition.

**Nothing about the position is stored.** Every input is already a column:
`ticket.created_at` starts both timers, `first_response_at` and `resolved_at` stop them,
and the target comes from the tenant's policy for the ticket's priority. A stored
`sla_due_at` would be a second copy of a fact that follows from those, and it would go
stale the moment a ticket was reprioritised — the exact hazard `notification_service`
names when it refuses to read structured data back out of `ticket_events.from_value`.

**What *is* stored is what the sweep has already said.** "Have we warned about this
already?" is not derivable from anything, so it is recorded — as a `TicketEvent`, which is
where a human would look for it anyway. `index_alerts` reads those rows back, and the
sweep and the API both go through it, so the record a client displays and the guard that
stops a second alert are the same rows.

**The clock does not know about status, and that is deliberate.** `resolve_position` never
reads `ticket.status`: it answers "how much time was there and how much is left", which is
true regardless of where the ticket is in its lifecycle. Whether a terminal ticket's
overdue resolution is *worth an alert* is the sweep's judgement, and the sweep makes it by
filtering to non-terminal tickets before asking. A clock that consulted status would be a
clock with a policy in it.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.core.permissions import Permission
from app.core.tenancy import RequestOrigin, TenantContext
from app.models.enums import AuditAction, TicketEventType, TicketPriority
from app.models.sla_policy import SLAPolicy
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.repositories.sla_policy_repository import SLAPolicyRepository
from app.repositories.ticket_event_repository import TicketEventRepository
from app.schemas.sla import (
    SLAPolicyRead,
    SLAPolicyUpdate,
    SLATimer,
    SLATimerRead,
    SLATimerState,
    TicketSLARead,
)
from app.services import audit_service

# ---------------------------------------------------------------------------
# §27's sample configuration
# ---------------------------------------------------------------------------

# `(priority, response minutes, resolution minutes)`, transcribed from §27's table. Hours
# converted to the minutes the column stores.
#
# **§27's closing paragraph is why this comment exists**: "Do not claim these values
# represent real industry standards; they are sample configuration for the application."
# They are seeded so a new tenant's clock works from its first ticket rather than being
# inert until somebody opens a settings screen, and every one of them is editable through
# `PATCH /sla/policies/{priority}`.
#
# The four rows satisfy `resolution_time_minutes >= response_time_minutes` by
# construction — 4320/1440, 1440/480, 480/120, 240/30 — which the table's own
# `resolution_after_response` CheckConstraint would reject at insert if it were ever
# edited carelessly. `tests/api/test_sla_policies.py` asserts all four land.
DEFAULT_POLICIES: tuple[tuple[TicketPriority, int, int], ...] = (
    (TicketPriority.LOW, 24 * 60, 72 * 60),
    (TicketPriority.MEDIUM, 8 * 60, 24 * 60),
    (TicketPriority.HIGH, 2 * 60, 8 * 60),
    (TicketPriority.URGENT, 30, 4 * 60),
)


# ---------------------------------------------------------------------------
# The position
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimerPosition:
    """Where one of a ticket's two clocks stands."""

    timer: SLATimer
    state: SLATimerState
    due_at: datetime
    # Signed, and relative to `stopped_at` when the timer has stopped — so a met timer
    # reads as time to spare and a late one as minutes overdue — and to `now` otherwise.
    remaining_seconds: int
    stopped_at: datetime | None


@dataclass(frozen=True, slots=True)
class SLAPosition:
    """Both clocks, plus the policy they were measured against."""

    policy: SLAPolicy
    response: TimerPosition
    resolution: TimerPosition


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


def _warning_offset(target_minutes: int, warning_threshold_percent: int) -> timedelta:
    """How long after a timer starts its warning band begins.

    The one expression of "what is a warning instant", used by `resolve_timer` for both
    timers and by `earliest_alert_offset` below. Two copies of this would be two answers
    to "when does a warning become due", and the sweep and the API would disagree about
    the one instant that decides whether an alert fires.
    """
    return timedelta(minutes=target_minutes) * (warning_threshold_percent / 100)


def earliest_alert_offset(policy: SLAPolicy) -> timedelta:
    """The soonest any alert could be due for a ticket on this policy, from its creation.

    The response timer's warning instant, and it bounds both timers: the table's
    `resolution_after_response` CheckConstraint makes the resolution target the longer of
    the two, and `SLAPolicyUpdate`'s threshold is a single field applying to both, so a
    ticket whose *resolution* warning is due has necessarily already passed its response
    warning. One bound therefore serves the sweep's candidate query — which is why
    `sla_repository.find_pending` takes a single `created_before` and not two.

    **This exists so the sweep does not re-derive it.** A worker that computed it from
    `policy.response_time_minutes` itself would be a second implementation of the clock
    outside the module that owns it, and the two would agree until one of them changed.
    """
    return _warning_offset(policy.response_time_minutes, policy.warning_threshold_percent)


def resolve_timer(
    timer: SLATimer,
    *,
    started_at: datetime,
    stopped_at: datetime | None,
    target_minutes: int,
    warning_threshold_percent: int,
    now: datetime,
) -> TimerPosition:
    """One timer's state, from five facts and nothing else.

    The branch order is the rule, in order of precedence:

    1. **Stopped** — the work happened. `MET` if it landed at or before the deadline and
       `BREACHED` if after. `<=` and not `<`, because the deadline instant itself is a
       moment you are still on time until it has passed.
    2. **Not stopped, past due** — `BREACHED`. `now >= due_at`, so the deadline instant is
       already a breach: at exactly `due_at` there is no time left to act in, which is what
       breaching means.
    3. **Not stopped, inside the warning band** — `WARNING`. The band is half-open,
       `[warning_at, due_at)`, so crossing the threshold counts and reaching the deadline
       does not — the deadline is case 2's, and a timer cannot be both.
    4. **Otherwise** — `ON_TRACK`.

    **Both datetimes must be timezone-aware.** `timestamptz` columns come back aware and
    every caller passes `datetime.now(UTC)`; a naive `now` against an aware `started_at`
    raises `TypeError` rather than silently comparing against the wrong offset, which is
    the failure mode worth having.

    `warning_threshold_percent` is bounded to 1 to 99 by the table's
    `warning_threshold_range` CheckConstraint and by `SLAPolicyUpdate`'s fields, so no
    value here can put the warning band outside `(started_at, due_at)`.
    """
    target = timedelta(minutes=target_minutes)
    due_at = started_at + target
    warning_at = started_at + _warning_offset(target_minutes, warning_threshold_percent)

    if stopped_at is not None:
        state = SLATimerState.MET if stopped_at <= due_at else SLATimerState.BREACHED
        reference = stopped_at
    elif now >= due_at:
        state = SLATimerState.BREACHED
        reference = now
    elif now >= warning_at:
        state = SLATimerState.WARNING
        reference = now
    else:
        state = SLATimerState.ON_TRACK
        reference = now

    return TimerPosition(
        timer=timer,
        state=state,
        due_at=due_at,
        remaining_seconds=int((due_at - reference).total_seconds()),
        stopped_at=stopped_at,
    )


def resolve_position(ticket: Ticket, policy: SLAPolicy, *, now: datetime) -> SLAPosition:
    """Where both of a ticket's clocks stand, right now.

    Both timers start at `ticket.created_at`. §27 gives bare durations ("first response:
    24 hours") and no start point, and creation is the only instant the two can share and
    the only one a policy can be applied to without reconstructing history.

    Wall-clock throughout — no business hours, no calendar, and no pause. §27 asks for
    neither, and `WAITING_FOR_CUSTOMER` does not stop the resolution clock: pausing means
    storing accumulated pause time, which is a schema change and a rule the specification
    does not state. Recorded as a limitation in the README rather than settled silently
    here.

    **`ticket.status` is not read.** See the module docstring: the clock reports what is
    true, and the sweep decides what is worth saying.
    """
    return SLAPosition(
        policy=policy,
        response=resolve_timer(
            SLATimer.RESPONSE,
            started_at=ticket.created_at,
            # Set by `message_service.post_reply` on the first *public* staff reply, which
            # is a first response and an internal note is not. That writer is Phase K's and
            # this phase does not touch it.
            stopped_at=ticket.first_response_at,
            target_minutes=policy.response_time_minutes,
            warning_threshold_percent=policy.warning_threshold_percent,
            now=now,
        ),
        resolution=resolve_timer(
            SLATimer.RESOLUTION,
            started_at=ticket.created_at,
            # Set by `ticket_service._transition` on a resolution and cleared by
            # `reopen_ticket`, whose docstring says leaving it "would make every SLA and
            # duration query wrong" — this is the query it meant.
            stopped_at=ticket.resolved_at,
            target_minutes=policy.resolution_time_minutes,
            warning_threshold_percent=policy.warning_threshold_percent,
            now=now,
        ),
    )


# ---------------------------------------------------------------------------
# What has already been said
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedAlerts:
    """The alerts already on one ticket's timeline, by timer.

    Empty is the common case and means "the sweep has said nothing about this ticket",
    which is not the same as "the sweep will say nothing" — a fresh ticket is in this
    state for its whole on-track life.
    """

    warnings: Mapping[SLATimer, datetime] = field(default_factory=dict)
    breaches: Mapping[SLATimer, datetime] = field(default_factory=dict)


def _timer_of(event: TicketEvent) -> SLATimer | None:
    """Which timer an SLA entry is about, or `None` if the row does not say.

    `extra_data` is JSONB and loose by design — the model calls it "anything else the
    specific event type needs to render" — so this reads defensively rather than trusting
    a shape. The only writer is `app/workers/sla_tasks.py`, which always sets it.
    """
    value = (event.extra_data or {}).get("timer")
    if not isinstance(value, str):
        return None
    try:
        return SLATimer(value)
    except ValueError:
        return None


def index_alerts(events: Iterable[TicketEvent]) -> dict[uuid.UUID, RecordedAlerts]:
    """Group timeline entries by ticket and timer. Pure, and shared by both readers.

    The sweep asks it "have we warned about this timer yet", and the API asks it "when did
    we warn" — one pass over one list answers both, which is why the two can never
    disagree about what has been said.

    An entry whose timer cannot be read is recorded against **both** timers. The only rows
    that can look like that are ones this application did not write, and the choice is
    between a missing alert and a repeating one: a duplicate fires on every sweep forever,
    which is what makes people stop reading notifications, while a missed alert is visible
    to the person who did not get it. Erring toward silence is the reading that degrades
    the smaller failure.
    """
    indexed: dict[uuid.UUID, RecordedAlerts] = {}

    for event in events:
        current = indexed.get(event.ticket_id) or RecordedAlerts()
        warnings = dict(current.warnings)
        breaches = dict(current.breaches)

        target = warnings if event.event_type is TicketEventType.SLA_WARNING else breaches
        timer = _timer_of(event)
        for which in (SLATimer.RESPONSE, SLATimer.RESOLUTION) if timer is None else (timer,):
            # Earliest wins. The guard means a second entry for one timer should not
            # exist, and if one ever did, the first is what the recipient was told.
            target[which] = min(event.created_at, target.get(which, event.created_at))

        indexed[event.ticket_id] = RecordedAlerts(warnings=warnings, breaches=breaches)

    return indexed


@dataclass(frozen=True, slots=True)
class SLAAlert:
    """An alert this ticket has newly earned, in the timeline's own vocabulary.

    `event_type` rather than a `NotificationType` because the timeline entry is written
    first and the notification is derived from it — `notification_service` holds the
    mapping, since a notification type is that module's word and not this one's.
    """

    timer: SLATimer
    event_type: TicketEventType
    due_at: datetime


def due_alerts(position: SLAPosition, alerts: RecordedAlerts) -> list[SLAAlert]:
    """What this ticket is now due to be told, given what it has already been told. Pure.

    The sweep's judgement, and it is a pure function for the same reason the clock is:
    "is this worth saying" must have one answer, and a rule evaluated inside a loop that
    also writes rows is a rule nobody can test without a database.

    **A warning and a breach are independent.** Each is guarded by its own recorded set,
    so the four possible alerts on a ticket are four separate decisions rather than a
    state machine. The guard is what makes the sweep idempotent: running it twice tells
    nobody anything the second time.

    **A breached timer never gets its warning afterwards.** The branches are exclusive and
    the state is monotone — `now` only advances and a stopped timer stays stopped — so a
    timer that was already past its deadline the first time the sweep looked at it sends
    the breach and never the warning. That is the right way round: "you have 20 minutes"
    arriving after the deadline would be worse than the miss it describes, and the warning
    exists to give somebody time to act.

    **A timer that stopped late still breaches.** `resolve_timer` reports `BREACHED` for a
    stopped-past-due timer, and this does not exclude them. The alternative loses the alert
    entirely: an agent replying at 35 minutes against a 30-minute target, between two
    sweeps, would leave a ticket that reads breached in the API, has no `breached_at`, and
    never told a manager anything — and §28's compliance metric counts that as a miss. A
    breach alert that arrives after the fact is still the record of a failure.
    """
    due: list[SLAAlert] = []

    for timer in (position.response, position.resolution):
        if timer.state is SLATimerState.WARNING:
            if timer.timer not in alerts.warnings:
                due.append(
                    SLAAlert(
                        timer=timer.timer,
                        event_type=TicketEventType.SLA_WARNING,
                        due_at=timer.due_at,
                    )
                )
        elif timer.state is SLATimerState.BREACHED and timer.timer not in alerts.breaches:
            due.append(
                SLAAlert(
                    timer=timer.timer,
                    event_type=TicketEventType.SLA_BREACHED,
                    due_at=timer.due_at,
                )
            )

    return due


def to_read(position: SLAPosition, alerts: RecordedAlerts) -> TicketSLARead:
    """Render a position as the API's `TicketSLARead`.

    The timestamps come from the timeline rather than from a fresh computation: `warned_at`
    is when the sweep actually fired, which is a fact, not a schedule.
    """

    def timer_read(timer: TimerPosition) -> SLATimerRead:
        return SLATimerRead(
            timer=timer.timer,
            state=timer.state,
            due_at=timer.due_at,
            remaining_seconds=timer.remaining_seconds,
            stopped_at=timer.stopped_at,
            warned_at=alerts.warnings.get(timer.timer),
            breached_at=alerts.breaches.get(timer.timer),
        )

    return TicketSLARead(
        response=timer_read(position.response),
        resolution=timer_read(position.resolution),
        policy=SLAPolicyRead.model_validate(position.policy),
    )


# ---------------------------------------------------------------------------
# Request-scoped reads
# ---------------------------------------------------------------------------


async def load_policies(
    session: AsyncSession, context: TenantContext
) -> dict[TicketPriority, SLAPolicy]:
    """This organization's active policies, keyed by priority.

    Inactive rows are dropped, so a priority with `is_active = false` is simply absent
    from the mapping — the same answer as a priority that was never configured, which is
    what lets `decorate` treat both without a second branch.
    """
    policies = await SLAPolicyRepository(session, context).list_active()
    return {policy.priority: policy for policy in policies}


async def list_policies(session: AsyncSession, context: TenantContext) -> Sequence[SLAPolicy]:
    """All four, active or not, in priority order — the admin's settings screen."""
    return await SLAPolicyRepository(session, context).list_all()


async def decorate(
    session: AsyncSession, context: TenantContext, tickets: Sequence[Ticket]
) -> dict[uuid.UUID, TicketSLARead]:
    """The SLA position of a page of tickets, keyed by ticket id.

    **Two queries for the whole page, whatever its size.** The policies are four rows read
    once, and the timeline entries come back in one `IN (…)` — the unbatched version of
    this is two queries per ticket, which behind a hundred-ticket `GET /tickets` is two
    hundred round trips for a field most callers will not render. The same reasoning that
    made `/notifications/unread-count` a `COUNT`.

    **Empty for a caller without `SLA_VIEW`**, which is §3's matrix: admin, manager, and
    agent hold it and customer does not. Returning `{}` rather than raising means
    `TicketRead.sla` is simply `null` for a portal caller — the list endpoint is shared by
    all four roles and refusing it would break the customer portal to hide one field.

    A ticket whose priority has no active policy is absent from the result for the same
    reason: `null`, not a fabricated position. A client cannot distinguish that from the
    authorization case, which is deliberate — see `TicketSLARead`'s docstring.
    """
    if not tickets or not context.has(Permission.SLA_VIEW):
        return {}

    policies = await load_policies(session, context)
    if not policies:
        return {}

    events = await TicketEventRepository(session, context).list_alerts_for_tickets(
        [ticket.id for ticket in tickets]
    )
    alerts = index_alerts(events)

    now = datetime.now(UTC)
    positions: dict[uuid.UUID, TicketSLARead] = {}
    for ticket in tickets:
        policy = policies.get(ticket.priority)
        if policy is None:
            continue
        positions[ticket.id] = to_read(
            resolve_position(ticket, policy, now=now),
            alerts.get(ticket.id) or RecordedAlerts(),
        )
    return positions


async def update_policy(
    session: AsyncSession,
    context: TenantContext,
    priority: TicketPriority,
    payload: SLAPolicyUpdate,
    *,
    origin: RequestOrigin | None = None,
) -> SLAPolicy:
    """Apply a partial edit to one priority's policy, and record it.

    **The cross-field rule is checked on the merged row.** `SLAPolicyUpdate`'s bounds
    mirror the table's per-field CheckConstraints, but `resolution_time_minutes >=
    response_time_minutes` relates two fields to each other, and a partial payload only
    supplies one of them. So the values are merged onto the stored row and *then*
    validated — which is what turns "raised the response target past an untouched
    resolution target" into a 422 naming the reason, instead of a `CheckViolationError`
    from PostgreSQL at commit that reaches the client as a 500.

    The audit row is written even when nothing changed. §34 names `SLA_POLICY_UPDATED`, and
    a trail that recorded only effective changes could not answer "did anyone touch the
    SLA configuration before the incident" — which is the question a trail exists for. A
    no-op edit leaves identical `before` and `after` on the row, which reads as exactly
    what it was.
    """
    repository = SLAPolicyRepository(session, context)
    policy = await repository.get_for_priority(priority)

    before = _snapshot(policy)
    changes = payload.model_dump(exclude_unset=True)

    merged_response = changes.get("response_time_minutes", policy.response_time_minutes)
    merged_resolution = changes.get("resolution_time_minutes", policy.resolution_time_minutes)
    if merged_resolution < merged_response:
        raise ValidationError(
            "A resolution target cannot be shorter than the response target — the ticket "
            "would breach resolution while still being on time for a first reply."
        )

    for name, value in changes.items():
        setattr(policy, name, value)

    audit_service.record_for(
        session,
        context,
        AuditAction.SLA_POLICY_UPDATED,
        target_type="sla_policy",
        target_id=policy.id,
        before=before,
        after=_snapshot(policy),
        origin=origin,
    )
    await session.commit()
    return policy


def _snapshot(policy: SLAPolicy) -> dict[str, Any]:
    """The four editable fields, for an audit row's `before` and `after`.

    `priority` and `id` are omitted because neither is editable — the endpoint addresses a
    policy *by* its priority, so recording it would put the same value on both sides of
    every row.
    """
    return {
        "response_time_minutes": policy.response_time_minutes,
        "resolution_time_minutes": policy.resolution_time_minutes,
        "warning_threshold_percent": policy.warning_threshold_percent,
        "is_active": policy.is_active,
    }


def build_default_policies(organization_id: uuid.UUID) -> list[SLAPolicy]:
    """§27's four rows for a new organization. **The caller commits.**

    Returned rather than inserted so this stays a pure constructor and the caller — which
    is `auth_service.register`, mid-transaction, with no `TenantContext` to build a
    repository from — can `session.add` them alongside the organization and its founding
    admin. All three land or none do, which is the same argument registration's own
    docstring makes about the organization and its user.
    """
    return [
        SLAPolicy(
            organization_id=organization_id,
            priority=priority,
            response_time_minutes=response_minutes,
            resolution_time_minutes=resolution_minutes,
        )
        for priority, response_minutes, resolution_minutes in DEFAULT_POLICIES
    ]
