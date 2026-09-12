"""The ticket lifecycle, asserted against `docs/requirements.md` §5 edge by edge.

`can_transition` is the guard that stands between a support desk and a ticket in a
state nobody can reason about. It is a small function over a small table, which is
exactly why it is worth testing exhaustively: the table is transcribed from the
specification, and a transcription is the kind of thing that is wrong in one cell
without anyone noticing.

The permitted edges are asserted **in full**, and every other pair of the 36 is
asserted refused. A test that checked only the permitted edges would pass against a
`can_transition` that returned `True` unconditionally.
"""

import itertools

import pytest

from app.models.enums import TICKET_TRANSITIONS, TicketStatus, can_transition

pytestmark = pytest.mark.unit

# Transcribed from docs/requirements.md §5. Written out here rather than imported from
# the same module under test — a test that read `TICKET_TRANSITIONS` and compared it
# with itself would pass no matter what the table said.
PERMITTED: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.OPEN: {TicketStatus.ASSIGNED},
    TicketStatus.ASSIGNED: {TicketStatus.IN_PROGRESS},
    TicketStatus.IN_PROGRESS: {TicketStatus.WAITING_FOR_CUSTOMER, TicketStatus.RESOLVED},
    TicketStatus.WAITING_FOR_CUSTOMER: {TicketStatus.IN_PROGRESS},
    TicketStatus.RESOLVED: {TicketStatus.CLOSED},
    # Reopening is an explicit action, never a side effect of another change.
    TicketStatus.CLOSED: {TicketStatus.OPEN},
}


@pytest.mark.parametrize(
    ("current", "target"),
    sorted((current, target) for current, targets in PERMITTED.items() for target in targets),
)
def test_the_permitted_edges_are_permitted(current: TicketStatus, target: TicketStatus) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    sorted(
        (current, target)
        for current, target in itertools.product(TicketStatus, TicketStatus)
        if target not in PERMITTED[current]
    ),
)
def test_every_other_pair_is_refused(current: TicketStatus, target: TicketStatus) -> None:
    """Including the self-transitions.

    `OPEN → OPEN` is refused deliberately. A no-op transition that "succeeded" would
    write a `status_changed` event recording that nothing changed, and would let a
    caller pass a status through unchanged while believing they had moved the ticket.
    The service returns early on an unchanged priority for the same reason.
    """
    assert not can_transition(current, target)


def test_the_table_covers_every_status() -> None:
    """Every status is a key, so a status added later cannot silently have no edges.

    A missing key is not an error to `.get` — it reads as "no edges from here", which
    is indistinguishable from a deliberate dead end. This makes adding a status to the
    enum without deciding its edges a failure rather than a trap.
    """
    assert set(TICKET_TRANSITIONS) == set(TicketStatus)


def test_the_edges_match_the_specification_s_table() -> None:
    """The transcription check, in one assertion.

    The two parametrised tests above already prove agreement edge by edge; this states
    it once so a failure reads as "the table changed" rather than as one of thirty
    parametrised case ids.
    """
    assert {current: set(targets) for current, targets in TICKET_TRANSITIONS.items()} == PERMITTED


def test_no_status_transitions_to_itself() -> None:
    """Stated separately from the parametrised refusal above, because it is a rule
    rather than an absence: the lifecycle is a chain of distinct states."""
    for status in TicketStatus:
        assert not can_transition(status, status), status


def test_a_closed_ticket_reopens_only_to_open() -> None:
    """The one cycle in an otherwise acyclic graph, and it is bounded.

    Every other status can only move forwards, so the graph would be acyclic without
    this edge. Reopening returns to `OPEN` rather than to `ASSIGNED` or
    `IN_PROGRESS`, which means a reopened ticket re-enters the lifecycle at the
    beginning — it has to be assigned again rather than silently reappearing in
    someone's in-progress queue.
    """
    assert TICKET_TRANSITIONS[TicketStatus.CLOSED] == frozenset({TicketStatus.OPEN})
    assert not can_transition(TicketStatus.CLOSED, TicketStatus.IN_PROGRESS)
    assert not can_transition(TicketStatus.CLOSED, TicketStatus.ASSIGNED)
