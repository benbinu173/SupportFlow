"""SLA vocabulary, and the shapes the API presents a ticket's clock in.

Two enums here, and the difference between them is why neither is in
`app/models/enums.py`:

* `SLATimerState` is **derived on every read and stored nowhere**. The module it would
  otherwise join opens with "Persisted as native PostgreSQL enum types so the database
  rejects invalid values rather than trusting the application" — and the database cannot
  reject a value it never holds. Putting it there would be a claim about the schema that
  the schema does not make.

* `SLATimer` *is* persisted, but as a string inside `ticket_events.extra_data`, which is
  JSONB. It is a value vocabulary rather than a column type, so the same argument applies
  from the other direction: adding it to the database enum module would suggest an
  `ALTER TYPE` that is not needed and will never be written.

Both live here because this is where a reader looks for the SLA vocabulary, and the
paragraph above is the answer to the one question a reader will have about the location.

**Nothing in this module computes anything.** The states below are produced by
`app/services/sla_service.py`, which is pure, and the timestamps are read off rows the
sweep wrote. A schema that could derive a deadline would be a second implementation of the
clock, which is the failure this whole design is arranged to avoid.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import TicketPriority


class SLATimer(StrEnum):
    """Which of a ticket's two clocks. Persisted in `ticket_events.extra_data`."""

    RESPONSE = "response"
    RESOLUTION = "resolution"


class SLATimerState(StrEnum):
    """Where a timer stands. Derived — see the module docstring.

    Four members, and the fourth is the one worth a word. `BREACHED` covers both "the
    deadline has passed and nothing has stopped the clock" and "the clock was stopped
    after the deadline". Those are different situations and the same verdict: §28's
    compliance metric counts a late resolution as a miss, and a client that needs to tell
    the two apart has `stopped_at` to do it with — non-null means the work happened, late.

    `MET` and `BREACHED` are only reachable once a timer has stopped. `ON_TRACK` and
    `WARNING` are only reachable while it has not, because a stopped clock has no band to
    be inside. That exhaustiveness is asserted in `tests/unit/test_sla_clock.py`.
    """

    ON_TRACK = "on_track"
    WARNING = "warning"
    BREACHED = "breached"
    MET = "met"


class SLATimerRead(BaseModel):
    """One of a ticket's two clocks, as a client renders it."""

    # Present even though the parent object's keys already name it: a countdown widget
    # collects `[sla.response, sla.resolution]` into a list and sorts by `due_at`, and
    # once flattened the two are otherwise indistinguishable.
    timer: SLATimer
    state: SLATimerState

    due_at: datetime
    # Signed, and the sign is the point. Relative to `stopped_at` when the timer has
    # stopped — "resolved with 2 hours to spare", "resolved 40 minutes late" — and to now
    # when it has not. Positive is time remaining or time to spare; negative is overdue.
    remaining_seconds: int

    # The column that ended the timer: `first_response_at` for the response timer,
    # `resolved_at` for the resolution timer. `None` means the clock is still running.
    stopped_at: datetime | None = None

    # When the sweep recorded each alert, read off the timeline entry's own `created_at`
    # rather than recomputed. Never set means the sweep has not fired for this timer —
    # which is not the same as "it will not", and a client showing a warning state should
    # read `state`, not this.
    warned_at: datetime | None = None
    breached_at: datetime | None = None


class SLAPolicyRead(BaseModel):
    """One priority's configured targets."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    priority: TicketPriority
    response_time_minutes: int
    resolution_time_minutes: int
    warning_threshold_percent: int
    is_active: bool


class TicketSLARead(BaseModel):
    """A ticket's SLA position, nested inside `TicketRead`.

    Absent — `None` — for two different reasons, and a client cannot tell them apart:
    the caller does not hold `SLA_VIEW` (§3 gives it to admin, manager, and agent and not
    to customer), or the organization has no active policy for this ticket's priority.
    Keeping them indistinguishable is deliberate: the second is a fact about the tenant's
    configuration, and the first is an authorization decision, and a response that
    distinguished them would tell a customer which priorities their provider has targets
    for. `tests/api/test_ticket_sla.py` asserts the customer case directly, because the
    route-protection sweep cannot — `TicketRead` is shared by all four roles.
    """

    response: SLATimerRead
    resolution: SLATimerRead
    policy: SLAPolicyRead


class SLAPolicyUpdate(BaseModel):
    """A partial edit to one priority's policy.

    **Every field is optional, and that is what makes this a `PATCH`.** The realistic edit
    is toggling `is_active` or moving one target, and an all-optional replace would be a
    `PUT` whose body is usually a smaller object than the row it replaces — which is the
    kind of mismatch this project names rather than ships.

    The bounds mirror the table's own `CheckConstraint`s exactly, so a value that would
    violate one is a 422 with a field name rather than an integrity error at commit.
    `resolution >= response` is not expressible here, because it relates two fields to a
    *stored* third state; `sla_service.update_policy` validates the merged row instead.
    """

    # `ge=1` is `response_time_positive` / `resolution_time_positive`. The ceiling is
    # this module's own addition and is not a product opinion: the column is a PostgreSQL
    # `integer`, and a value past 2^31-1 would be a `DataError` at commit rather than a
    # validation refusal. One year is far beyond any real target — §27's longest is 72
    # hours — and well inside the type.
    response_time_minutes: int | None = Field(default=None, ge=1, le=525_600)
    resolution_time_minutes: int | None = Field(default=None, ge=1, le=525_600)

    # `warning_threshold_range` is `> 0 AND < 100`, so 1 to 99. A threshold of 0 would
    # warn at the instant the ticket was created and 100 would warn at the breach, which
    # is the same moment as the breach alert — the constraint rules out both.
    warning_threshold_percent: int | None = Field(default=None, ge=1, le=99)

    is_active: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "SLAPolicyUpdate":
        """Refuse an empty body.

        Pydantic is happy with `{}` — every field is optional — so without this the route
        would commit nothing, write an audit row claiming a policy was updated, and answer
        `200`. `model_fields_set` is the right question and not "are all values `None`",
        because `{"is_active": null}` is a well-formed request that names a field and
        supplies no value, which is a `422` from the field's own type rather than from
        here.
        """
        if not self.model_fields_set:
            raise ValueError("Supply at least one field to change.")
        return self
