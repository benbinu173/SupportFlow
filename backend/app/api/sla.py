"""SLA endpoints — the policy an organization runs on.

Two routes, and the asymmetry between their guards is §3's matrix rather than a choice
made here. `GET /sla/policies` is `SLA_VIEW`, which admin, manager, and agent all hold: an
agent working to a deadline needs to know what the deadline is. `PATCH
/sla/policies/{priority}` is `SLA_CONFIGURE`, which is admin only — the targets are a
business commitment, and §3 gives the capability that sets them to exactly one role.

**There is deliberately no route that reports a ticket's position from here.** A ticket's
SLA position is a field on the ticket (`GET /tickets/{id}` and its list), computed at read
time by `app/services/sla_service.py`. A `/sla/tickets/{id}` alongside it would be a second
path to the same number with its own copy of the authorization decision, and the two would
agree until one changed.

The queue-wide view — §31's "SLA risks" and §28's `GET /analytics/sla` — is not here
either. Both need the deadline arithmetic expressed in SQL to sort and paginate by it, and
a second implementation of the clock that agrees with the pure one until one of them is
edited is the specific failure this phase is arranged to avoid. Both arrive in Phase S,
where they will call `resolve_position` per row or build the query on top of it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.deps import Context, DbSession, Origin, require_permission
from app.core.permissions import Permission
from app.models.enums import TicketPriority
from app.schemas.sla import SLAPolicyRead, SLAPolicyUpdate
from app.services import sla_service

router = APIRouter()

# Declared once and attached per route rather than hoisted into a shared list: the two
# routes need *different* capabilities, which is the whole point of splitting a settings
# screen between "read the targets" and "set them". `tests/security/test_route_protection.py`
# walks the routing table and would catch a third route arriving without one.
_view = [Depends(require_permission(Permission.SLA_VIEW))]
_configure = [Depends(require_permission(Permission.SLA_CONFIGURE))]


@router.get(
    "/policies",
    response_model=list[SLAPolicyRead],
    summary="The organization's SLA policies",
    dependencies=_view,
)
async def list_policies(context: Context, db: DbSession) -> list[SLAPolicyRead]:
    """Every priority's targets, in `LOW, MEDIUM, HIGH, URGENT` order.

    A list rather than an object keyed by priority, and ordered by the priority enum's
    declaration order rather than by anything the caller sends. An admin screen renders
    them in that order and a client should not have to know it — the same reasoning that
    makes `TicketSortKey.PRIORITY` sort by declaration order, and equally worth stating.

    **Inactive policies are included.** A settings screen has to show a target the tenant
    switched off in order to offer switching it back on; hiding it would make `is_active`
    a one-way door. The clock ignores them, which is a different question from whether an
    admin can see them.

    Every organization is seeded with all four at registration, so this returns four rows
    for any tenant created by `POST /auth/register`.
    """
    policies = await sla_service.list_policies(db, context)
    return [SLAPolicyRead.model_validate(policy) for policy in policies]


@router.patch(
    "/policies/{priority}",
    response_model=SLAPolicyRead,
    summary="Change one priority's SLA targets",
    dependencies=_configure,
)
async def update_policy(
    priority: Annotated[TicketPriority, Path(description="Which policy to edit.")],
    payload: SLAPolicyUpdate,
    context: Context,
    db: DbSession,
    origin: Origin,
) -> SLAPolicyRead:
    """Apply a partial edit, and record it in the audit trail.

    `PATCH` and not `PUT`: every field is optional and an omitted one keeps its stored
    value. A replace-shaped `PUT` would require the caller to resend three fields to flip
    `is_active`, and would report values the request supplied rather than values the
    database holds.

    **Two rules are enforced and neither is a shape check.** At least one field must be
    present — an empty body is a request that asks for nothing, and answering `200` would
    claim to have done something. And `resolution_time_minutes >= response_time_minutes`
    is validated against the **merged** row, so raising only the response target past an
    untouched resolution target is a `422` with a sentence rather than a database
    `CheckViolationError` surfacing as a `500`.

    A priority with no row is `404`. Every tenant is seeded with all four, so this means
    rows removed outside the application; the endpoint edits a policy that exists rather
    than inventing one from a request body.
    """
    policy = await sla_service.update_policy(db, context, priority, payload, origin=origin)
    return SLAPolicyRead.model_validate(policy)
