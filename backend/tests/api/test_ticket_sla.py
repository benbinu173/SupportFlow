"""`TicketRead.sla` over HTTP: the position a staff member sees, and the one a customer does not.

This is the read half of Phase Q. The clock itself is proved pure and exhaustively in
`tests/unit/test_sla_clock.py`; what can only be asserted *here* is the wiring — that the
two read routes decorate their responses, that the decoration agrees with the clock for a
ticket whose history is real, and that §3's matrix is honoured on the one field of
`TicketRead` that is not the same for all four roles.

**The customer case is the reason this file exists.** `TicketRead` is shared by all four
roles and `{**ticket}`-shaped except for this one field, so the difference between "the
field is populated" and "the field is null for the portal" is a single `if` inside
`sla_service.decorate` that no route-protection sweep can see — both roles hold
`TICKET_VIEW`, and the route is the same route. A regression that dropped the check would
show a customer the provider's response target for their priority, which is the fact
`TicketSLARead`'s docstring says the null is there to protect.

**The clock is moved by moving the ticket, never by mocking `datetime`.** Every test that
needs an old ticket backdates `created_at` in SQL. That works because the position is
derived rather than stored: there is no cache to invalidate and no second copy to keep in
step, and a `created_at` far enough in the past is a ticket that has been open that long
as far as every reader is concerned. Patching the clock instead would assert that
`resolve_position` is called with a substituted `now`, which the unit test already proves,
rather than that the API reports what the clock says.

**Two absences are asserted through the API rather than the database**, because both are
absences in a response the caller can see: a customer's `sla` is `null`, and a priority
whose policy is switched off produces `null` for everybody.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.conftest import SLA, TICKETS, OrgSession

pytestmark = pytest.mark.integration

# `HIGH`'s seeded targets, §27's table: 120 minutes to a first response, 480 to a
# resolution, warning at 80% — so the response warning band opens at 96 minutes and the
# resolution clock is nowhere near its own warning until 384. Every backdating test below
# picks a minute count that lands one timer in a band and leaves the other where it is,
# which is what makes "the two clocks are independent" observable rather than asserted.
RESPONSE_MINUTES = 120
RESOLUTION_MINUTES = 480
RESPONSE_WARNING_MINUTES = 96


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Clock Co")


@pytest.fixture
def manager(org: OrgSession) -> OrgSession:
    return org.add_user("manager", email="manager@clockco.com")


@pytest.fixture
def agent(org: OrgSession) -> OrgSession:
    """The assignee. Only needed to reach a status the lifecycle gates behind `ASSIGNED`.

    Which is not a detail of this test's setup but of the model: `OPEN → ASSIGNED` is the
    edge assignment owns, and `IN_PROGRESS` is only reachable from `ASSIGNED`, so a ticket
    cannot be resolved without first having been given to somebody.
    """
    return org.add_user("agent", email="agent@clockco.com")


@pytest.fixture
def portal(org: OrgSession) -> dict[str, Any]:
    """A customer record plus one portal login linked to it."""
    record = org.add_customer(name="Grace Hopper", email="grace@navy.mil")
    return {"record": record, "session": org.add_portal_user(cast("str", record["id"]))}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fetch(session: OrgSession, ticket_id: object) -> dict[str, Any]:
    response = session.get(f"{TICKETS}/{ticket_id}")
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def sla_of(session: OrgSession, ticket_id: object) -> dict[str, Any] | None:
    return cast("dict[str, Any] | None", fetch(session, ticket_id)["sla"])


def listed(session: OrgSession) -> list[dict[str, Any]]:
    response = session.get(TICKETS)
    assert response.status_code == 200, response.text
    return cast("list[dict[str, Any]]", response.json())


def raise_ticket(session: OrgSession, customer_id: object, **kwargs: Any) -> dict[str, Any]:
    return session.add_ticket(cast("str", customer_id), priority="high", **kwargs)


def backdate(engine: Engine, ticket_id: object, *, minutes: int) -> None:
    """Move a ticket's creation into the past, which is the only way to age the clock.

    `make_interval(mins => …)` rather than a literal, so the count stays an integer
    parameter and the statement stays parameterized. Nothing else about the ticket is
    touched: `first_response_at` and `resolved_at` are stops, and this test ages tickets
    that have not been stopped yet unless it deliberately stops them first.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tickets SET created_at = created_at - make_interval(mins => :minutes) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(ticket_id), "minutes": minutes},
        )


def reply(session: OrgSession, ticket_id: object) -> None:
    """A public staff reply, which is what stops the response clock.

    Sent by the manager rather than by an agent so no assignment is needed: both roles map
    to `SenderType.AGENT`, and a manager's row scope reaches every ticket in the tenant.
    """
    response = session.post(f"{TICKETS}/{ticket_id}/messages", json={"body": "Looking at it."})
    assert response.status_code == 201, response.text


def resolve(session: OrgSession, assignee: OrgSession, ticket_id: object) -> None:
    """Walk the lifecycle's own edges to `RESOLVED`, which is where `resolved_at` is set.

    Three requests rather than a direct status write, because `POST /tickets/{id}/status`
    refuses the edges assignment owns and there is no route that jumps the lifecycle. The
    assignment is a real one — to a real agent in this organization — because
    `assign_ticket` validates the recipient, and unassigning an `ASSIGNED` ticket is
    refused outright.
    """
    assigned = session.post(
        f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": assignee.user_id}
    )
    assert assigned.status_code == 200, assigned.text
    for status in ("in_progress", "resolved"):
        response = session.post(f"{TICKETS}/{ticket_id}/status", json={"status": status})
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


def test_a_ticket_carries_both_clocks_and_the_policy_they_were_measured_against(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """Three objects, and the third is what makes the other two checkable by a reader.

    A client rendering "2h 14m left" has to say *of what*, and the policy is where the
    target it is counting down to comes from. Reporting the position without the policy
    would leave the numbers unverifiable except against a second request.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])

    sla = sla_of(manager, ticket["id"])

    assert sla is not None
    assert set(sla) == {"response", "resolution", "policy"}
    assert sla["policy"]["priority"] == "high"
    assert sla["policy"]["response_time_minutes"] == RESPONSE_MINUTES
    assert sla["policy"]["resolution_time_minutes"] == RESOLUTION_MINUTES


def test_each_timer_names_itself(manager: OrgSession, portal: dict[str, Any]) -> None:
    """`response` and `resolution` on the object, and again on each half.

    Redundant in the JSON and asserted anyway: a client that collects the two into a list
    to sort by `due_at` — which is the obvious way to render a countdown widget — has no
    other way to tell which one it is looking at once the keys are gone.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])

    sla = sla_of(manager, ticket["id"])
    assert sla is not None
    assert sla["response"]["timer"] == "response"
    assert sla["resolution"]["timer"] == "resolution"


def test_the_deadline_is_the_policy_target_after_creation(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """`due_at - created_at` is exactly the target, to the second.

    Exact rather than approximate, and that is the assertion: both sides come from the
    same stored `created_at` and the same stored target, so any drift between them would
    mean the API had computed a deadline from something else — a fresh `now`, a cached
    policy, a default. A tolerance here would hide precisely the bug worth finding.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])

    sla = sla_of(manager, ticket["id"])
    assert sla is not None

    created = datetime.fromisoformat(ticket["created_at"])
    for timer, target in (("response", RESPONSE_MINUTES), ("resolution", RESOLUTION_MINUTES)):
        due = datetime.fromisoformat(sla[timer]["due_at"])
        assert (due - created).total_seconds() == target * 60


def test_a_fresh_ticket_is_on_track_and_owes_nothing(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """The state every ticket starts in, with no alert timestamps on either clock.

    `warned_at` and `breached_at` are absent rather than zeroed because nothing has been
    said yet — and the assertion is what makes them "the sweep's record" rather than "the
    state's shadow". A client showing a warning badge reads `state`; one showing *when* the
    warning fired reads these, and only the second is a fact rather than a computation.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])

    sla = sla_of(manager, ticket["id"])
    assert sla is not None

    for timer in ("response", "resolution"):
        assert sla[timer]["state"] == "on_track"
        assert sla[timer]["stopped_at"] is None
        assert sla[timer]["warned_at"] is None
        assert sla[timer]["breached_at"] is None
        assert sla[timer]["remaining_seconds"] > 0


# ---------------------------------------------------------------------------
# The states, from a ticket the clock actually moved through
# ---------------------------------------------------------------------------


def test_inside_the_warning_band_the_response_clock_reads_warning(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """Past 96 minutes and short of 120, the response timer is in the band.

    The resolution clock is asserted in the same test because it is the control: 100
    minutes is nowhere near its own warning at 384, and the two being different answers for
    one ticket is what "two independent timers" means. A single shared state would pass a
    test that only looked at the response.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    backdate(sync_engine, ticket["id"], minutes=RESPONSE_WARNING_MINUTES + 4)

    sla = sla_of(manager, ticket["id"])
    assert sla is not None

    assert sla["response"]["state"] == "warning"
    assert 0 < sla["response"]["remaining_seconds"] < (RESPONSE_MINUTES - 96) * 60
    assert sla["resolution"]["state"] == "on_track"


def test_past_the_deadline_the_response_clock_reads_breached(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """Past 120 minutes, the timer is breached and `remaining_seconds` has gone negative.

    The sign is asserted rather than the magnitude: it is the difference between "two
    minutes left" and "two minutes late", and a client rendering an unsigned countdown
    would show the second as the first. This is the field's whole reason for being signed.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    backdate(sync_engine, ticket["id"], minutes=RESPONSE_MINUTES + 10)

    sla = sla_of(manager, ticket["id"])
    assert sla is not None

    assert sla["response"]["state"] == "breached"
    assert sla["response"]["remaining_seconds"] < 0
    assert sla["response"]["stopped_at"] is None


def test_a_reply_inside_the_target_reads_met_with_time_to_spare(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """A stopped timer reports the work, not the wall clock.

    `remaining_seconds` is measured to `stopped_at` once there is one, so a ticket replied
    to promptly reads as hours to spare forever after — rather than as an ever-shrinking
    number that would eventually go negative on work that was already done. This is the
    case that would be wrong if the reference were always `now`.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    reply(manager, ticket["id"])

    sla = sla_of(manager, ticket["id"])
    assert sla is not None

    assert sla["response"]["state"] == "met"
    assert sla["response"]["stopped_at"] is not None
    assert sla["response"]["remaining_seconds"] > (RESPONSE_MINUTES - 1) * 60


def test_a_reply_after_the_deadline_reads_breached_with_a_stop(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """Stopped, and late: `BREACHED` with a non-null `stopped_at`.

    The two are not contradictory and a client needs both — "the first response arrived, 40
    minutes after it was due" is a different sentence from "nobody has replied". The API
    distinguishes them by the stop being present, which is exactly what
    `SLATimerState.BREACHED`'s docstring says `stopped_at` is for.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    backdate(sync_engine, ticket["id"], minutes=RESPONSE_MINUTES + 40)
    reply(manager, ticket["id"])

    sla = sla_of(manager, ticket["id"])
    assert sla is not None

    assert sla["response"]["state"] == "breached"
    assert sla["response"]["stopped_at"] is not None
    assert sla["response"]["remaining_seconds"] < 0


def test_resolving_stops_the_resolution_clock_and_leaves_the_other_running(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any]
) -> None:
    """Two clocks, two stops, and resolving touches exactly one of them.

    A resolution with no reply leaves the response timer running, and that is correct
    rather than a gap: nobody answered the customer, so there was no first response, and a
    clock that stopped on `resolved_at` would be reporting a reply that never happened.
    The ticket is terminal, so the sweep will not alert about it — which is why the
    position is allowed to say something the sweep would not act on.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    resolve(manager, agent, ticket["id"])

    sla = sla_of(manager, ticket["id"])
    assert sla is not None

    assert sla["resolution"]["state"] == "met"
    assert sla["resolution"]["stopped_at"] is not None
    assert sla["response"]["state"] == "on_track"
    assert sla["response"]["stopped_at"] is None


def test_a_breached_ticket_still_reads_breached_after_it_is_resolved(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """The miss is not erased by the ticket being finished.

    This is §28's compliance metric in miniature: a ticket that breached its first-response
    target and was then resolved on time has to keep reporting the breach, or the only
    thing a dashboard could ever count is open work. `resolved_at` stops the resolution
    clock and says nothing about the response one.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    backdate(sync_engine, ticket["id"], minutes=RESPONSE_MINUTES + 40)
    reply(manager, ticket["id"])
    resolve(manager, agent, ticket["id"])

    sla = sla_of(manager, ticket["id"])
    assert sla is not None

    assert sla["response"]["state"] == "breached"
    assert sla["response"]["stopped_at"] is not None
    assert sla["resolution"]["state"] == "met"


def test_reopening_a_ticket_clears_its_resolution_stop(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any]
) -> None:
    """`reopen_ticket` clears `resolved_at`, and the clock reads that as running again.

    Asserted here as well as in the unit test because the clearing and the reading are in
    different modules by different phases: `ticket_service` clears the column because
    leaving it "would make every SLA and duration query wrong", and this is the query it
    meant. A future change to either half has to break this test.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    resolve(manager, agent, ticket["id"])
    closed = manager.post(f"{TICKETS}/{ticket['id']}/close")
    assert closed.status_code == 200, closed.text
    reopened = manager.post(f"{TICKETS}/{ticket['id']}/reopen")
    assert reopened.status_code == 200, reopened.text

    sla = sla_of(manager, ticket["id"])
    assert sla is not None

    assert sla["resolution"]["stopped_at"] is None
    assert sla["response"]["stopped_at"] is None


# ---------------------------------------------------------------------------
# Who gets one
# ---------------------------------------------------------------------------


def test_a_customer_gets_no_sla_object(manager: OrgSession, portal: dict[str, Any]) -> None:
    """The portal's `sla` is `null` — the assertion this file exists for.

    §3 gives `SLA_VIEW` to admin, manager, and agent. The ticket itself is visible: the
    portal login is linked to the customer that owns it, so the 200 and the populated
    `subject` below are what make the null mean "withheld" rather than "not found" — a
    portal user who could not reach the ticket at all would satisfy a null check by
    accident, and for the wrong reason.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    customer: OrgSession = portal["session"]

    body = fetch(customer, ticket["id"])

    assert body["subject"] == ticket["subject"]
    assert body["sla"] is None


def test_the_list_gives_a_staff_member_every_position_and_a_customer_none(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """The same rule on the list route, which is a separate call site for the decoration.

    Two routes and two `decorate` calls, so a fix applied to one is not evidence about the
    other. The staff side is asserted as "every row has one" rather than "the response has
    a key", because the failure worth catching here is a page where the decoration was
    computed and then not attached.
    """
    raise_ticket(manager, portal["record"]["id"], subject="First")
    raise_ticket(manager, portal["record"]["id"], subject="Second")
    customer: OrgSession = portal["session"]

    assert all(row["sla"] is not None for row in listed(manager))
    assert [row["sla"] for row in listed(customer)] == [None, None]


def test_both_read_routes_answer_the_portal_with_200(
    manager: OrgSession, portal: dict[str, Any]
) -> None:
    """A customer with nothing to see still gets 200 from the list, not 403.

    `decorate` returning `{}` rather than raising is a design decision — the list endpoint
    is shared by all four roles and refusing it would break the portal to hide one field —
    and the decision has a visible consequence: the portal's requests succeed and the field
    is null. Worth pinning, because the alternative implementation ("the portal is refused
    these routes") is plausible-looking, would pass every test above, and would break the
    product.
    """
    customer: OrgSession = portal["session"]

    assert customer.get(TICKETS).status_code == 200


def test_every_route_that_returns_a_ticket_returns_its_clock(
    manager: OrgSession, agent: OrgSession, portal: dict[str, Any]
) -> None:
    """Not just the two read routes: the mutations carry it too, and the portal's is null.

    Eight routes return a `TicketRead` and seven of them are not reads. The first draft of
    this phase decorated only `GET /tickets` and `GET /tickets/{id}`, on the reasoning that
    the other six echo the ticket back rather than reporting it — and that is wrong in a
    way worth a test, because `TicketRead.sla` has a default of `None`. An undecorated
    response therefore does not *omit* the field, it serializes `"sla": null` — the same
    payload a portal caller gets. A client doing `setTicket(await assign(...))` would watch
    the countdown vanish on assign and have no way to tell that from the authorization
    case, and every test above would still pass.

    Every route is exercised on one ticket, in the order the lifecycle permits, because
    the failure this catches is a call site that forgot the helper — and a call site is
    exactly what a test of the *read* routes cannot see. Creation is included: it is the
    first payload a client ever renders, and it was the undecorated one that made the bug
    visible in the first place.

    The portal half is the same rule from the other side. Its mutation responses are
    `null` too — a customer closing their own ticket is a `TICKET_CLOSE` holder, and the
    withholding is `SLA_VIEW`'s, not the route's.
    """
    created = raise_ticket(manager, portal["record"]["id"])
    assert created["sla"] is not None, "the creation response"

    assigned = manager.post(
        f"{TICKETS}/{created['id']}/assign", json={"assigned_agent_id": agent.user_id}
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["sla"] is not None, "assignment"

    reprioritised = manager.post(f"{TICKETS}/{created['id']}/priority", json={"priority": "urgent"})
    assert reprioritised.status_code == 200, reprioritised.text
    assert reprioritised.json()["sla"] is not None, "a priority change"

    for status in ("in_progress", "resolved"):
        moved = manager.post(f"{TICKETS}/{created['id']}/status", json={"status": status})
        assert moved.status_code == 200, moved.text
        assert moved.json()["sla"] is not None, f"the move to {status}"

    # The portal closes — a customer agreeing the fix worked is the `TICKET_CLOSE` holder
    # — and the manager reopens. Interleaved rather than grouped so that both roles are
    # asserted on a route the other one also uses, which is what makes the null a fact
    # about the capability rather than about which endpoint was called.
    customer: OrgSession = portal["session"]
    theirs = customer.post(f"{TICKETS}/{created['id']}/close")
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["sla"] is None, "the portal's closing response"

    reopened = manager.post(f"{TICKETS}/{created['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["sla"] is not None, "reopening"


def test_a_priority_with_no_active_policy_reads_as_no_position(
    org: OrgSession, manager: OrgSession, portal: dict[str, Any]
) -> None:
    """Switching a priority off makes its tickets' `sla` null for everybody, admin included.

    Which is the second meaning of the null, and the reason a client cannot tell the two
    apart. It is also the only way to observe that the clock ignores inactive rows without
    waiting on the sweep: `load_policies` drops them, `decorate` finds nothing to measure
    against, and the field is absent rather than fabricated as on-track.

    The switch is flipped by the admin, because `SLA_CONFIGURE` is admin-only — a manager
    who could do it here would be a manager who could do it in production.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    assert sla_of(manager, ticket["id"]) is not None

    updated = org.patch(f"{SLA}/policies/high", json={"is_active": False})
    assert updated.status_code == 200, updated.text

    assert sla_of(org, ticket["id"]) is None
    assert sla_of(manager, ticket["id"]) is None
    assert sla_of(portal["session"], ticket["id"]) is None


# ---------------------------------------------------------------------------
# The timeline
# ---------------------------------------------------------------------------


def test_the_timeline_hides_sla_entries_from_a_customer(
    manager: OrgSession, portal: dict[str, Any], sync_engine: Engine
) -> None:
    """A deadline entry is the policy, so the capability that reads the policy reads these.

    The rows are inserted rather than produced, because producing one needs the sweep and
    the sweep needs a backdated ticket and a loop of its own — `tests/integration/test_sla_sweep.py`
    does that for real. What this test is about is the *filter* in
    `TicketEventRepository.list_for_ticket`, and a row written directly is the same row the
    sweep writes as far as the filter is concerned: same event type, same nullable actor,
    same JSONB.

    The customer's timeline is read in full below the SLA assertion, so the emptiness is
    provably about these two rows rather than about an endpoint that returned nothing. That
    distinction is the whole test — the ticket was created, so the customer's timeline has
    at least a `created` entry.

    **The two rows are asserted as a set, and the reason is worth knowing.** Both are
    inserted in one transaction, so both get the same `created_at` — PostgreSQL's `now()`
    is transaction time — and `list_for_ticket` orders by `(created_at, id)`, which leaves
    two same-instant rows in uuid order. The sweep writes the same way: at most two alerts
    per ticket per pass, sharing an instant. Nothing reads that order (the guard takes the
    earliest per timer, and equal timestamps are equal either way), so it is not a defect —
    but a test asserting chronological order here would be asserting a coin flip.
    """
    ticket = raise_ticket(manager, portal["record"]["id"])
    with sync_engine.begin() as conn:
        for event_type, timer in (("sla_warning", "response"), ("sla_breached", "response")):
            conn.execute(
                text(
                    "INSERT INTO ticket_events "
                    "(organization_id, ticket_id, event_type, actor_user_id, extra_data) "
                    "SELECT organization_id, id, CAST(:event_type AS ticket_event_type), "
                    "NULL, jsonb_build_object('timer', CAST(:timer AS text)) "
                    "FROM tickets WHERE id = CAST(:id AS uuid)"
                ),
                {"id": str(ticket["id"]), "event_type": event_type, "timer": timer},
            )

    staff_events = manager.get(f"{TICKETS}/{ticket['id']}/events")
    assert staff_events.status_code == 200, staff_events.text
    assert sorted(row["event_type"] for row in staff_events.json()) == [
        "created",
        "sla_breached",
        "sla_warning",
    ]

    portal_events = portal["session"].get(f"{TICKETS}/{ticket['id']}/events")
    assert portal_events.status_code == 200, portal_events.text
    assert [row["event_type"] for row in portal_events.json()] == ["created"]
