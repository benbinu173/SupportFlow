"""The SLA clock — every state, both boundaries, and the rules that are easy to get wrong.

**No database, and that is the point of the file.** `resolve_position` is pure by design:
it takes a ticket, a policy, and a `now`, and returns where both clocks stand (ADR-024).
Everything asserted here is a fact about arithmetic on five values, and a test that needed
a session to check "exactly at the deadline is a breach" would be testing the ORM as well
as the rule. The one thing the clock reads off a timeline row — which timer an alert was
about — is exercised through `index_alerts`, which is why its defensive branch has a case
below rather than being taken on trust.

The two boundaries are the reason this file exists at all. `due_at` and `warning_at` are
instants, and whether each is *inside* its band is decided by a `>=` or a `>` that reads
identically when you are skimming and differently when a ticket breaches one second late.
So both sides of both boundaries are asserted, one second apart, rather than described.
"""

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import TicketEventType, TicketPriority, TicketStatus
from app.models.sla_policy import SLAPolicy
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.schemas.sla import SLATimer, SLATimerState
from app.services.sla_service import (
    DEFAULT_POLICIES,
    RecordedAlerts,
    SLAPosition,
    due_alerts,
    earliest_alert_offset,
    index_alerts,
    resolve_position,
    to_read,
)

pytestmark = pytest.mark.unit

# An arbitrary fixed instant. Every test builds its datetimes relative to this, so the
# arithmetic is readable — "twenty minutes before the deadline" rather than five numbers
# that have to be checked by hand.
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _policy(
    *,
    priority: TicketPriority = TicketPriority.HIGH,
    response_minutes: int = 120,
    resolution_minutes: int = 480,
    warning_threshold_percent: int = 80,
    is_active: bool = True,
) -> SLAPolicy:
    """An unsaved policy. Never flushed, so no session is needed to build one.

    HIGH's §27 targets by default — two hours to respond, eight to resolve, warning at 80%
    — which puts the warning at 96 minutes and the deadline at 120. Both round numbers, so
    a test that says "two minutes past the warning" is checkable without a calculator.
    """
    return SLAPolicy(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        priority=priority,
        response_time_minutes=response_minutes,
        resolution_time_minutes=resolution_minutes,
        warning_threshold_percent=warning_threshold_percent,
        is_active=is_active,
    )


def _ticket(
    policy: SLAPolicy,
    *,
    age_minutes: int = 0,
    first_response_minutes: int | None = None,
    resolved_minutes: int | None = None,
    status: TicketStatus = TicketStatus.IN_PROGRESS,
    priority: TicketPriority | None = None,
) -> Ticket:
    """An unsaved ticket, `age_minutes` old at `NOW`.

    The stop timestamps are given as *minutes after creation* rather than as datetimes,
    because that is how the assertions read: "replied at 121 minutes" against a 120-minute
    target is obviously late, while two absolute timestamps have to be subtracted first.
    """
    created_at = NOW - timedelta(minutes=age_minutes)
    return Ticket(
        id=uuid.uuid4(),
        organization_id=policy.organization_id,
        number=1042,
        customer_id=uuid.uuid4(),
        subject="The printer is on fire",
        description="It really is.",
        status=status,
        priority=priority or policy.priority,
        created_at=created_at,
        first_response_at=(
            None
            if first_response_minutes is None
            else created_at + timedelta(minutes=first_response_minutes)
        ),
        resolved_at=(
            None if resolved_minutes is None else created_at + timedelta(minutes=resolved_minutes)
        ),
    )


def _alert(
    ticket_id: uuid.UUID,
    event_type: TicketEventType,
    *,
    timer: str | None = SLATimer.RESPONSE.value,
    created_at: datetime = NOW,
    extra_data: dict[str, object] | None = None,
) -> TicketEvent:
    """An unsaved timeline entry, for the alert index.

    `extra_data` is overridable so the unreadable-timer branches can be built deliberately;
    `timer=None` drops the key entirely, which is the shape a row written by anything other
    than `sla_tasks` would have.
    """
    data: dict[str, object] = {} if timer is None else {"timer": timer}
    if extra_data is not None:
        data = extra_data
    return TicketEvent(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        ticket_id=ticket_id,
        event_type=event_type,
        extra_data=data,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# The boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset_minutes", "expected"),
    [
        # The response timer: 120 minutes, warning at 80% = 96.
        (0, SLATimerState.ON_TRACK),
        (95, SLATimerState.ON_TRACK),
        # The warning instant itself is inside the band. Half-open `[warning_at, due_at)`.
        (96, SLATimerState.WARNING),
        (119, SLATimerState.WARNING),
        # The deadline instant itself is a breach: at `due_at` there is no time left to act
        # in, which is what breaching means. One second earlier is still a warning.
        (120, SLATimerState.BREACHED),
        (121, SLATimerState.BREACHED),
    ],
)
def test_the_response_band_boundaries(offset_minutes: int, expected: SLATimerState) -> None:
    """Four bands, six points, both boundaries inclusive on the side that matters.

    The failure this catches is an off-by-one in either comparison. `now >= warning_at`
    written as `>` loses the warning at exactly 96 minutes; `now >= due_at` written as `>`
    lets a ticket sit past its deadline reporting "on track" until the next second ticks.
    Neither is visible in a test that only checks the middle of a band.
    """
    policy = _policy()
    position = resolve_position(_ticket(policy, age_minutes=offset_minutes), policy, now=NOW)

    assert position.response.state is expected


def test_the_deadline_instant_has_no_time_remaining() -> None:
    """At `due_at`, `remaining_seconds` is exactly zero — the arithmetic behind case 2.

    Asserting the state alone would pass even if `remaining_seconds` used a different
    reference instant than the state did, and a client rendering "0 seconds left" beside
    "breached" is the kind of contradiction this project has one implementation to avoid.
    """
    policy = _policy()
    position = resolve_position(_ticket(policy, age_minutes=120), policy, now=NOW)

    assert position.response.remaining_seconds == 0
    assert position.response.state is SLATimerState.BREACHED


def test_a_ticket_inside_the_warning_band_reports_the_time_it_has_left() -> None:
    """`remaining_seconds` is signed and counts down to the deadline, not through it.

    Relative to `now` while the clock runs. A ticket 100 minutes into a 120-minute target
    has 20 minutes left, which is the number an agent's countdown widget shows.
    """
    policy = _policy()
    position = resolve_position(_ticket(policy, age_minutes=100), policy, now=NOW)

    assert position.response.remaining_seconds == 20 * 60
    assert position.response.due_at == NOW + timedelta(minutes=20)


# ---------------------------------------------------------------------------
# Stopped timers
# ---------------------------------------------------------------------------


def test_a_response_within_the_target_is_met() -> None:
    """The first public staff reply stops the response clock, and stopping it in time is MET."""
    policy = _policy()
    position = resolve_position(
        _ticket(policy, age_minutes=200, first_response_minutes=30), policy, now=NOW
    )

    assert position.response.state is SLATimerState.MET
    assert position.response.stopped_at == NOW - timedelta(minutes=170)
    # Relative to the stop, not to now: "replied with 90 minutes to spare" stays true
    # however long the test runs afterwards.
    assert position.response.remaining_seconds == 90 * 60


def test_a_response_named_at_exactly_the_deadline_is_met() -> None:
    """The deadline instant counts as on time once the work has happened.

    `<=` here and `>=` for the running clock, and the asymmetry is deliberate: the question
    changes from "is there time left" to "did it land inside the window", and the instant
    itself belongs to the window in the second reading and not the first.
    """
    policy = _policy()
    position = resolve_position(
        _ticket(policy, age_minutes=200, first_response_minutes=120), policy, now=NOW
    )

    assert position.response.state is SLATimerState.MET
    assert position.response.remaining_seconds == 0


def test_a_late_response_is_a_breach_and_says_how_late() -> None:
    """One second past the deadline is a breach, and the negative remainder is the lateness.

    `BREACHED` covers both "past due and still running" and "stopped after the deadline".
    `stopped_at` is what tells them apart, which is why it is asserted here: a client
    showing "resolved 40 minutes late" needs that column and not the state.
    """
    policy = _policy()
    position = resolve_position(
        _ticket(policy, age_minutes=200, first_response_minutes=121), policy, now=NOW
    )

    assert position.response.state is SLATimerState.BREACHED
    assert position.response.stopped_at is not None
    assert position.response.remaining_seconds == -60


# ---------------------------------------------------------------------------
# The ticket the clock is asked about
# ---------------------------------------------------------------------------


def test_the_clock_does_not_read_the_ticket_status() -> None:
    """Two tickets identical but for their status produce identical positions.

    The module docstring claims the clock reports what is true and the sweep decides what
    is worth saying. That claim is only worth making if it is enforced, and the enforcement
    is that `resolve_position` never touches `status` — asserted by constructing the two
    tickets a status apart and comparing the whole position, not one field of it.

    What it buys: a resolved ticket's overdue resolution is still *reported* as breached, so
    §28's compliance metric and the ticket detail screen agree, while the sweep filters
    terminal tickets out before asking and so never alerts about one.
    """
    policy = _policy()
    running = resolve_position(
        _ticket(policy, age_minutes=600, status=TicketStatus.IN_PROGRESS), policy, now=NOW
    )
    closed = resolve_position(
        _ticket(policy, age_minutes=600, status=TicketStatus.CLOSED), policy, now=NOW
    )

    assert running == closed


def test_a_reopened_ticket_has_a_running_resolution_clock_again() -> None:
    """`reopen_ticket` clears `resolved_at`, and the clock follows the column.

    The reopen path's own docstring says leaving `resolved_at` set "would make every SLA and
    duration query wrong" — this is that query. A cleared timestamp is a running clock, and
    the breached-looking history does not survive as a stop.
    """
    policy = _policy()
    position = resolve_position(
        _ticket(policy, age_minutes=600, resolved_minutes=None), policy, now=NOW
    )

    assert position.resolution.stopped_at is None
    assert position.resolution.state is SLATimerState.BREACHED


def test_both_timers_are_measured_against_the_same_start() -> None:
    """`created_at` starts both clocks, and the two stops are independent.

    A ticket created 100 minutes ago with no reply has a response 20 minutes from breaching
    and a resolution 380 minutes from it. Reading both stops off one column would be the
    easy mistake; this pins them apart.
    """
    policy = _policy()
    position = resolve_position(_ticket(policy, age_minutes=100), policy, now=NOW)

    assert position.response.due_at == NOW + timedelta(minutes=20)
    assert position.resolution.due_at == NOW + timedelta(minutes=380)
    assert position.response.stopped_at is None
    assert position.resolution.stopped_at is None


def test_a_position_carries_the_policy_it_was_measured_against() -> None:
    """The rendered position reports the targets, so a client can show "120m of 480m".

    Asserted because `TicketSLARead.policy` exists for exactly this and the position is
    where it comes from — a client has no other way to learn the target, since the deadline
    alone does not say when the clock started.
    """
    policy = _policy()
    position = resolve_position(_ticket(policy, age_minutes=10), policy, now=NOW)

    assert position.policy is policy


def test_the_position_is_a_function_of_its_inputs() -> None:
    """Same ticket, same policy, same `now` — same answer. Twice, and by signature.

    Two claims, and the second is the one that matters. The equality below proves little on
    its own — a function with a `datetime.now()` default would usually agree with itself —
    while the signature is what makes the guarantee real: `now` is a required keyword-only
    argument with no default, so there is no path through this function that reads a clock
    of its own. If a default ever reappears, this fails on the signature rather than on a
    flaky comparison.
    """
    policy = _policy()
    ticket = _ticket(policy, age_minutes=97)

    assert resolve_position(ticket, policy, now=NOW) == resolve_position(ticket, policy, now=NOW)

    parameter = inspect.signature(resolve_position).parameters["now"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# The sweep's candidate bound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("priority", "response_minutes", "resolution_minutes"), DEFAULT_POLICIES)
def test_the_candidate_bound_is_the_response_warning(
    priority: TicketPriority, response_minutes: int, resolution_minutes: int
) -> None:
    """`earliest_alert_offset` is 80% of the response target for every seeded policy.

    Parametrized over §27's four rows rather than one, because the property is about all of
    them: the sweep asks a single `created_before` per priority, and it is correct only if
    no alert can come due earlier than that bound.
    """
    policy = _policy(
        priority=priority,
        response_minutes=response_minutes,
        resolution_minutes=resolution_minutes,
    )

    assert earliest_alert_offset(policy) == timedelta(minutes=response_minutes) * 0.8


@pytest.mark.parametrize(("priority", "response_minutes", "resolution_minutes"), DEFAULT_POLICIES)
def test_no_alert_can_come_due_before_the_candidate_bound(
    priority: TicketPriority, response_minutes: int, resolution_minutes: int
) -> None:
    """Neither timer's warning can precede `earliest_alert_offset` — the bound's whole job.

    Checked by asking the clock rather than by comparing the two targets, so this follows
    the clock if its arithmetic changes. It is the resolution timer that would break it:
    the table's `resolution_after_response` CheckConstraint makes the resolution target the
    longer one, so its warning lands later — and if that constraint were ever dropped, this
    test is what would catch the sweep silently skipping tickets.
    """
    policy = _policy(
        priority=priority,
        response_minutes=response_minutes,
        resolution_minutes=resolution_minutes,
    )
    # One second before the bound: nothing may be due yet.
    position = resolve_position(
        _ticket(policy, age_minutes=0),
        policy,
        now=NOW + earliest_alert_offset(policy) - timedelta(seconds=1),
    )

    assert due_alerts(position, RecordedAlerts()) == []


# ---------------------------------------------------------------------------
# What is newly due
# ---------------------------------------------------------------------------


def _position(
    age_minutes: int,
    *,
    policy: SLAPolicy | None = None,
    first_response_minutes: int | None = None,
    resolved_minutes: int | None = None,
) -> SLAPosition:
    """A position for a ticket of a given age, against the default HIGH policy."""
    policy = policy or _policy()
    return resolve_position(
        _ticket(
            policy,
            age_minutes=age_minutes,
            first_response_minutes=first_response_minutes,
            resolved_minutes=resolved_minutes,
        ),
        policy,
        now=NOW,
    )


def test_a_ticket_on_track_is_due_nothing() -> None:
    """The common case, and the one that must stay free: a fresh ticket writes no rows."""
    assert due_alerts(_position(age_minutes=10), RecordedAlerts()) == []


def test_a_ticket_in_the_warning_band_owes_a_warning() -> None:
    """Inside the band and nothing recorded means the response warning is due."""
    (alert,) = due_alerts(_position(age_minutes=100), RecordedAlerts())

    assert alert.timer is SLATimer.RESPONSE
    assert alert.event_type is TicketEventType.SLA_WARNING
    assert alert.due_at == NOW + timedelta(minutes=20)


def test_a_recorded_warning_is_not_repeated() -> None:
    """The guard, and the reason the sweep is idempotent.

    Without this the sweep would stage a warning on every run for the rest of the ticket's
    life — five minutes apart, forever — which is what makes people stop reading
    notifications, and would leave `warned_at` meaning nothing.
    """
    recorded = RecordedAlerts(warnings={SLATimer.RESPONSE: NOW - timedelta(minutes=5)})

    assert due_alerts(_position(age_minutes=100), recorded) == []


def test_a_breached_timer_owes_a_breach() -> None:
    """Past the deadline with nothing recorded means the breach is due."""
    (alert,) = due_alerts(_position(age_minutes=200), RecordedAlerts())

    assert alert.timer is SLATimer.RESPONSE
    assert alert.event_type is TicketEventType.SLA_BREACHED
    assert alert.due_at == NOW - timedelta(minutes=80)


def test_a_breached_timer_does_not_owe_its_warning() -> None:
    """A ticket first seen past its deadline gets the breach and never the warning.

    The state is monotone — `now` only advances — so a warning skipped this way is skipped
    permanently, and that is the right way round: "you have 20 minutes" arriving after the
    deadline is worse than useless. The case is worth asserting because the alternative
    reading ("backfill the warnings, they did happen") is a defensible-sounding one that
    would produce exactly that.
    """
    alerts = due_alerts(_position(age_minutes=200), RecordedAlerts())

    assert [alert.event_type for alert in alerts] == [TicketEventType.SLA_BREACHED]


def test_a_late_stop_still_owes_a_breach() -> None:
    """A timer stopped after its deadline is reported BREACHED, and the sweep says so.

    The case the sweep would otherwise lose entirely: an agent replies at 121 minutes
    against a 120-minute target, between two sweeps. Nothing is running any more, so a rule
    that only alerted on running clocks would leave the miss unrecorded — the ticket would
    read breached in the API with no `breached_at`, and §28 would count a failure nobody
    was told about.
    """
    (alert,) = due_alerts(_position(age_minutes=200, first_response_minutes=121), RecordedAlerts())

    assert alert.timer is SLATimer.RESPONSE
    assert alert.event_type is TicketEventType.SLA_BREACHED


def test_a_breached_timer_that_was_warned_still_owes_its_breach() -> None:
    """Warned at 96 minutes, breached at 120 — two alerts, and the second is not a correction.

    This is the decision §26 left open: the breach is a second notification, not an
    amendment of the first, because both statements were true when they were made.
    """
    recorded = RecordedAlerts(warnings={SLATimer.RESPONSE: NOW - timedelta(minutes=25)})
    (alert,) = due_alerts(_position(age_minutes=200), recorded)

    assert alert.event_type is TicketEventType.SLA_BREACHED


def test_both_timers_can_owe_an_alert_at_once() -> None:
    """Two alerts from one ticket, in a fixed order, each with its own deadline.

    An URGENT ticket left alone for three hours is past both deadlines. The order is
    response then resolution — the timers' declaration order — and the two `due_at` values
    differ, which is what keeps them apart once the rows are on the timeline.
    """
    policy = _policy(priority=TicketPriority.URGENT, response_minutes=30, resolution_minutes=240)
    alerts = due_alerts(_position(age_minutes=300, policy=policy), RecordedAlerts())

    assert [(alert.timer, alert.event_type) for alert in alerts] == [
        (SLATimer.RESPONSE, TicketEventType.SLA_BREACHED),
        (SLATimer.RESOLUTION, TicketEventType.SLA_BREACHED),
    ]
    assert alerts[0].due_at < alerts[1].due_at


def test_the_sweep_produces_at_most_four_alerts_per_ticket() -> None:
    """The at-most-four property, asserted by running the rule forward through time.

    Each pass feeds its own output back in as "already recorded", which is what the sweep
    does between ticks — the difference being that the sweep's record is a committed row
    and this is a dict. The instants below are chosen to cross all four lines on an URGENT
    ticket (30 minutes to respond, 240 to resolve, warning at 24 and 192): a response
    warning, a response breach, a resolution warning, a resolution breach, and then six
    hundred minutes of the ticket sitting there.

    Repeating is the point — an idempotency bug shows up on the *third* pass as often as
    the second — so the last assertion re-asks at the end of the timeline and must get
    nothing. Two timers, two states, once each, forever: that bound is what makes the
    timeline's SLA entries countable and the alert volume a tenant can reason about.
    """
    policy = _policy(priority=TicketPriority.URGENT, response_minutes=30, resolution_minutes=240)
    # Created at NOW and never answered: both timers run, so the state at each instant
    # below is decided by the clock alone.
    ticket = _ticket(policy, age_minutes=0)
    recorded = RecordedAlerts()
    staged: list[tuple[SLATimer, TicketEventType]] = []

    for minutes_later in (10, 25, 40, 200, 300, 1000):
        position = resolve_position(ticket, policy, now=NOW + timedelta(minutes=minutes_later))
        due = due_alerts(position, recorded)
        if not due:
            continue
        warnings = dict(recorded.warnings)
        breaches = dict(recorded.breaches)
        for alert in due:
            staged.append((alert.timer, alert.event_type))
            target = warnings if alert.event_type is TicketEventType.SLA_WARNING else breaches
            target[alert.timer] = NOW + timedelta(minutes=minutes_later)
        recorded = RecordedAlerts(warnings=warnings, breaches=breaches)

    assert staged == [
        (SLATimer.RESPONSE, TicketEventType.SLA_WARNING),
        (SLATimer.RESPONSE, TicketEventType.SLA_BREACHED),
        (SLATimer.RESOLUTION, TicketEventType.SLA_WARNING),
        (SLATimer.RESOLUTION, TicketEventType.SLA_BREACHED),
    ]

    final = resolve_position(ticket, policy, now=NOW + timedelta(days=30))
    assert due_alerts(final, recorded) == []


# ---------------------------------------------------------------------------
# What has already been said
# ---------------------------------------------------------------------------


def test_alerts_are_grouped_by_ticket_and_timer() -> None:
    """One pass over the page's entries, indexed the way both readers ask for them.

    The sweep asks "has this timer been warned", the API asks "when" — and they read the
    same structure, which is what makes the guard and the displayed `warned_at` incapable of
    disagreeing.
    """
    first, second = uuid.uuid4(), uuid.uuid4()
    warned_at = NOW - timedelta(minutes=30)

    indexed = index_alerts(
        [
            _alert(first, TicketEventType.SLA_WARNING, created_at=warned_at),
            _alert(first, TicketEventType.SLA_BREACHED, timer=SLATimer.RESOLUTION.value),
            _alert(second, TicketEventType.SLA_WARNING, timer=SLATimer.RESOLUTION.value),
            _alert(second, TicketEventType.SLA_WARNING, created_at=NOW - timedelta(minutes=5)),
        ]
    )

    assert indexed[first].warnings == {SLATimer.RESPONSE: warned_at}
    assert indexed[first].breaches == {SLATimer.RESOLUTION: NOW}
    # `second` has one warning per timer, and its two WARNING entries land in the same map
    # keyed differently — which is the whole point of keying by timer rather than by type.
    assert indexed[second].warnings == {
        SLATimer.RESPONSE: NOW - timedelta(minutes=5),
        SLATimer.RESOLUTION: NOW,
    }
    assert indexed[second].breaches == {}


@pytest.mark.parametrize(
    "extra_data",
    [
        pytest.param({}, id="no-timer-key"),
        pytest.param({"timer": "sideways"}, id="not-a-timer"),
        pytest.param({"timer": 7}, id="not-a-string"),
    ],
)
def test_an_unreadable_timer_counts_as_both_timers(extra_data: dict[str, object]) -> None:
    """A row whose timer cannot be read suppresses both timers rather than repeating.

    `extra_data` is JSONB and loose by design, so this reads defensively. The choice is
    between a missing alert and a repeating one, and it errs toward silence: a duplicate
    fires on every sweep forever, while a missed alert is visible to the person who did not
    get it. The three cases are the shapes a row this application did not write can take.
    """
    ticket_id = uuid.uuid4()

    indexed = index_alerts([_alert(ticket_id, TicketEventType.SLA_WARNING, extra_data=extra_data)])

    assert indexed[ticket_id].warnings == {SLATimer.RESPONSE: NOW, SLATimer.RESOLUTION: NOW}


def test_the_earliest_alert_per_timer_is_the_one_reported() -> None:
    """Duplicate entries resolve to the first, which is what the recipient was told.

    The guard means a second entry for one timer should not exist. If one ever did — a
    hand-written row, a re-run against a database restored from an older backup — the
    reported `warned_at` must be the earlier fact and not the later one.
    """
    ticket_id = uuid.uuid4()
    earlier, later = NOW - timedelta(hours=2), NOW - timedelta(hours=1)

    indexed = index_alerts(
        [
            _alert(ticket_id, TicketEventType.SLA_WARNING, created_at=later),
            _alert(ticket_id, TicketEventType.SLA_WARNING, created_at=earlier),
        ]
    )

    assert indexed[ticket_id].warnings == {SLATimer.RESPONSE: earlier}


def test_an_empty_timeline_indexes_to_nothing() -> None:
    """The common case for a fresh page, and it must not raise or invent entries."""
    assert index_alerts([]) == {}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_the_rendered_position_reports_when_the_alerts_fired() -> None:
    """`warned_at` and `breached_at` come off the timeline, not from a fresh computation.

    A computed "when should the warning have fired" would report the schedule rather than
    what happened, and the two differ by up to one sweep interval — which is the difference
    an operator investigating an alert needs to see.
    """
    ticket_id = uuid.uuid4()
    ticket = _ticket(_policy(), age_minutes=200)
    ticket.id = ticket_id
    policy = _policy()
    warned_at = NOW - timedelta(minutes=105)
    position = resolve_position(ticket, policy, now=NOW)
    alerts = index_alerts(
        [
            _alert(ticket_id, TicketEventType.SLA_WARNING, created_at=warned_at),
            _alert(ticket_id, TicketEventType.SLA_BREACHED),
        ]
    )[ticket_id]

    read = to_read(position, alerts)

    assert read.response.state is SLATimerState.BREACHED
    assert read.response.warned_at == warned_at
    assert read.response.breached_at == NOW
    # The resolution timer has been told nothing, and null says exactly that.
    assert read.resolution.warned_at is None
    assert read.resolution.breached_at is None
    assert read.policy.priority is policy.priority
