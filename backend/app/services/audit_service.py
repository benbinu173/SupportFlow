"""The audit service — one writer for the organization-wide trail.

Spec §34 lists the actions worth recording and the fields each record should carry
(actor, organization, action, entity, timestamp, useful metadata, before/after values
where appropriate). This module is the single place those rows are built.

Three decisions shape it:

**It never commits.** Exactly like `ticket_service.record_event`, the audit row is
staged into the caller's open transaction. An audit trail written in a transaction of
its own can disagree with the data it describes — a rollback that undoes a ticket but
leaves the audit row claiming it was created is a trail that lies, and a trail that
lies answers no question. The cost is that a caller must call this *before* its commit,
which is why every wiring site is adjacent to the write it records.

**The actor comes from the context, never from an argument the caller can choose.** The
`context`-taking entry point reads the user id and email off the authenticated identity,
so a call site cannot stamp a different actor — the same reasoning that puts
`record_event`'s actor in the context rather than in a parameter. The lower-level
`record` exists only for the one case that has no request-scoped identity yet:
registration, which creates an organization and its founding administrator in the same
transaction.

**`before`/`after` go into `extra_data`, not into columns.** The spec asks for them
"where appropriate", and different actions have different shapes to record — a role
change has two strings, a status change has two statuses, a ticket creation has nothing
before it. A pair of JSONB keys holds all three without four nullable columns that are
empty for most rows.
"""

import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import RequestOrigin, TenantContext
from app.models.audit_log import AuditLog
from app.models.enums import AuditAction
from app.repositories.audit_log_repository import AuditLogRepository

logger = structlog.get_logger(__name__)


def record(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    actor_email: str | None,
    action: AuditAction,
    target_type: str,
    target_id: uuid.UUID | None,
    before: Any = None,
    after: Any = None,
    metadata: dict[str, Any] | None = None,
    origin: RequestOrigin | None = None,
) -> AuditLog:
    """Stage one audit row. **The caller commits.**

    `target_type` is a table name — `"ticket"`, `"user"`, `"customer"` — and `target_id`
    the row it names. There is deliberately no foreign key behind either (the model
    explains why: an audit row has to outlive the record it describes, and a
    `CASCADE` would delete the evidence of whatever did the deleting).

    `session.add` rather than a repository method: `AuditLogRepository` is constructed
    from a `TenantContext`, and the registration path that needs this primitive is
    precisely the one that does not have one yet. The row is a plain INSERT with no
    query built around it, so a repository would add a constructor requirement and
    nothing else.
    """
    extra_data: dict[str, Any] = {}
    if before is not None:
        extra_data["before"] = before
    if after is not None:
        extra_data["after"] = after
    if metadata:
        extra_data.update(metadata)

    row = AuditLog(
        organization_id=organization_id,
        action=action,
        # Both are recorded: the id for joins while the user exists, the email because
        # `actor_user_id` becomes NULL when that user is deleted, and "who did this"
        # then has no other answer.
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_type=target_type,
        target_id=target_id,
        # Always a dict, never `None`. The column is `nullable=False` with a `'{}'::jsonb`
        # server default, so a `None` here is a NOT NULL violation at insert — and
        # `none_as_null=True` means it would be a real SQL NULL rather than JSON `null`,
        # which is precisely what the constraint exists to catch. An action with nothing
        # to add records an empty object, which is the honest representation of "there
        # was no extra detail".
        extra_data=extra_data,
        ip_address=origin.ip_address if origin else None,
        user_agent=origin.user_agent if origin else None,
    )
    session.add(row)

    logger.info(
        "audit_recorded",
        action=str(action),
        organization_id=str(organization_id),
        actor_id=str(actor_user_id) if actor_user_id else None,
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
    )
    return row


def record_for(
    session: AsyncSession,
    context: TenantContext,
    action: AuditAction,
    *,
    target_type: str,
    target_id: uuid.UUID | None,
    before: Any = None,
    after: Any = None,
    metadata: dict[str, Any] | None = None,
    origin: RequestOrigin | None = None,
) -> AuditLog:
    """Stage one audit row for an authenticated request.

    The identity — organization, actor id, actor email — is read from the context and
    cannot be supplied by the caller. That is the whole point of this wrapper existing
    next to `record`: a wiring site passes the action and the target, and gets the actor
    right by construction.
    """
    return record(
        session,
        organization_id=context.organization_id,
        actor_user_id=context.user_id,
        actor_email=context.email,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        metadata=metadata,
        origin=origin,
    )


async def list_logs(
    session: AsyncSession,
    context: TenantContext,
    *,
    action: AuditAction | None,
    actor_user_id: uuid.UUID | None,
    target_type: str | None,
    target_id: uuid.UUID | None,
    created_after: datetime | None,
    created_before: datetime | None,
    limit: int,
    offset: int,
) -> list[AuditLog]:
    """A page of the caller's organization's audit trail, newest first.

    No scope narrowing beyond the tenant: §3's matrix gives "View audit log" to admin
    and to nobody else, and the route enforces that with `AUDIT_VIEW`. A second,
    row-level rule here would be a rule with exactly one role in it, which is how a
    capability check quietly becomes a special case.
    """
    return list(
        await AuditLogRepository(session, context).list_logs(
            action=action,
            actor_user_id=actor_user_id,
            target_type=target_type,
            target_id=target_id,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            offset=offset,
        )
    )
