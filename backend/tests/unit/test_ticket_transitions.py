"""Ticket lifecycle transition rules.

Pure logic, so no database. The table under test is the single source of truth the
service layer will enforce against — asserting it here means a change to the
allowed transitions cannot pass silently.
"""

import pytest
from app.models.enums import TICKET_TRANSITIONS, TicketStatus, can_transition

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TicketStatus.OPEN, TicketStatus.ASSIGNED),
        (TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS),
        (TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_CUSTOMER),
        (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
        (TicketStatus.WAITING_FOR_CUSTOMER, TicketStatus.IN_PROGRESS),
        (TicketStatus.RESOLVED, TicketStatus.CLOSED),
        (TicketStatus.CLOSED, TicketStatus.OPEN),
    ],
)
def test_permitted_transitions(current: TicketStatus, target: TicketStatus) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Skipping assignment would leave a ticket in progress with no owner.
        (TicketStatus.OPEN, TicketStatus.IN_PROGRESS),
        # Resolving without any work recorded.
        (TicketStatus.OPEN, TicketStatus.RESOLVED),
        # Closing without resolving skips the resolution timestamp SLAs rely on.
        (TicketStatus.IN_PROGRESS, TicketStatus.CLOSED),
        # Reopening is only permitted from closed, and only as an explicit action.
        (TicketStatus.RESOLVED, TicketStatus.IN_PROGRESS),
        # A customer reply on a resolved ticket must reopen it, not silently revert.
        (TicketStatus.RESOLVED, TicketStatus.WAITING_FOR_CUSTOMER),
    ],
)
def test_rejected_transitions(current: TicketStatus, target: TicketStatus) -> None:
    assert not can_transition(current, target)


@pytest.mark.parametrize("status", list(TicketStatus))
def test_no_status_transitions_to_itself(status: TicketStatus) -> None:
    """A no-op transition should be rejected, not treated as a state change.

    Allowing it would append a meaningless timeline event and could fire a
    notification for a change that did not happen.
    """
    assert not can_transition(status, status)


@pytest.mark.parametrize("status", list(TicketStatus))
def test_every_status_is_reachable(status: TicketStatus) -> None:
    """No state is stranded.

    `open` is the entry point; every other status must be some transition's target,
    or a ticket could never arrive there.
    """
    if status is TicketStatus.OPEN:
        return
    targets = {target for allowed in TICKET_TRANSITIONS.values() for target in allowed}
    assert status in targets


@pytest.mark.parametrize("status", list(TicketStatus))
def test_no_status_is_a_dead_end(status: TicketStatus) -> None:
    """Every state has a way out, including closed — which reopens."""
    assert TICKET_TRANSITIONS.get(status), f"{status} has no outbound transition"
