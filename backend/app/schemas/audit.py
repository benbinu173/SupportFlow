"""Audit log response schemas.

Read-only by construction: there is no create, update, or delete schema, because there
is no route that accepts one. An audit trail a client can write to is not an audit
trail — every row is written by `app/services/audit_service.py` from inside the
transaction of the action it records, and the only thing a client may do with one is
read it.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import AuditAction


class AuditLogRead(BaseModel):
    """One audit entry as the API presents it.

    `organization_id` is absent for the same reason it is absent from every other read
    model in this API: the caller already knows which organization they are in — the
    token says so — and echoing it back invites a client to believe it is a field it can
    influence.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: AuditAction

    # Both, deliberately. `actor_user_id` is the join key while the actor exists;
    # `actor_email` is what survives their deletion, which is when an audit row matters
    # most. `actor_user_id` is `None` for a system-initiated action.
    actor_user_id: uuid.UUID | None
    actor_email: str | None

    # The entity the action applied to. `target_type` is a table name and `target_id`
    # the row — with no foreign key behind either, so a row can describe a target that
    # has since been deleted, which is the case it exists for.
    target_type: str
    target_id: uuid.UUID | None

    # `before` and `after` keys when the action had a value to compare, plus any
    # action-specific detail. Never credentials, tokens, or password hashes.
    extra_data: dict[str, Any]

    # Request provenance, recorded for investigation and never used for a decision.
    # Both are client-controlled and neither is trustworthy as evidence on its own.
    ip_address: str | None
    user_agent: str | None

    created_at: datetime
