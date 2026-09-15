"""Audit log endpoints — the admin viewer.

One route, and it is a read. There is no create, update, or delete here because an audit
trail that a client can write is not an audit trail: every row is written by
`app/services/audit_service.py`, from inside the transaction of the action it describes.

Guarded by `AUDIT_VIEW`, which §3's matrix gives to admin and to nobody else. That is the
whole access rule — the repository narrows to the caller's organization and nothing
further, because a second row-level rule with exactly one role in it is how a capability
check quietly turns into a special case.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import Context, DbSession, require_permission
from app.core.permissions import Permission
from app.models.enums import AuditAction
from app.schemas.audit import AuditLogRead
from app.services import audit_service

router = APIRouter()


@router.get(
    "/audit-logs",
    response_model=list[AuditLogRead],
    summary="An organization's audit trail",
    dependencies=[Depends(require_permission(Permission.AUDIT_VIEW))],
)
async def list_audit_logs(
    context: Context,
    db: DbSession,
    action: Annotated[AuditAction | None, Query(description="Filter by action type.")] = None,
    actor_user_id: Annotated[
        uuid.UUID | None, Query(description="Everything one user did.")
    ] = None,
    target_type: Annotated[
        str | None, Query(description="Table name, e.g. 'ticket' or 'user'.")
    ] = None,
    target_id: Annotated[uuid.UUID | None, Query(description="One entity's history.")] = None,
    created_after: Annotated[
        datetime | None, Query(description="Inclusive lower bound on the timestamp.")
    ] = None,
    created_before: Annotated[
        datetime | None, Query(description="Exclusive upper bound on the timestamp.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditLogRead]:
    """A page of the caller's organization's audit trail, newest first.

    The filters answer the four questions an investigation actually asks, and each is
    backed by an index: what happened recently (`ix_audit_logs_org_created`), what did a
    particular person do (`ix_audit_logs_org_actor_created`), what happened to this
    entity (`ix_audit_logs_target`), and how often does this action occur
    (`ix_audit_logs_org_action_created`).

    The date range is half-open — `created_after` inclusive, `created_before` exclusive —
    so two adjacent windows can be walked without counting a boundary row twice. That
    matters more here than on a ticket list: an audit trail is read to establish a
    sequence of events, and a row appearing in two exports of it is a small lie about
    what happened.
    """
    logs = await audit_service.list_logs(
        db,
        context,
        action=action,
        actor_user_id=actor_user_id,
        target_type=target_type,
        target_id=target_id,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )
    return [AuditLogRead.model_validate(log) for log in logs]
