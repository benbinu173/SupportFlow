"""Notification endpoints — the in-app inbox.

Four routes, and none of them creates anything. Notifications are written by
`app/services/notification_service.py` from inside the transaction of the action they
describe; a client's only verbs here are "show me mine", "how many are unread", and two
ways of saying "read".

**Every route is guarded by `NOTIFICATION_LIST`, which all four roles hold.** §3's matrix
gives notifications to every role because a notification is addressed to a person and not
to a job — an admin gets them for the same reason an agent does. So the guard is uniform
and the narrowing is per-row: the repository restricts every query to
`user_id == context.user_id`, which is the only access rule here and cannot be widened by
a role.

**A colleague's notification is a 404, not a 403.** ADR-009, and the same body as an id
that was never written — otherwise the endpoint answers "does this id exist" for anyone
willing to walk a uuid space.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import Context, DbSession, require_permission
from app.core.permissions import Permission
from app.schemas.notification import (
    NotificationMarkAllResult,
    NotificationRead,
    NotificationUnreadCount,
)
from app.services import notification_service

router = APIRouter()

# Declared once and attached to all four routes. A capability named four times is four
# places to forget one; `tests/security/test_route_protection.py` walks the routing table
# and would catch a route that ended up without it, but only one of these should ever
# need editing.
_guard = [Depends(require_permission(Permission.NOTIFICATION_LIST))]


@router.get(
    "/notifications",
    response_model=list[NotificationRead],
    summary="The caller's notifications",
    dependencies=_guard,
)
async def list_notifications(
    context: Context,
    db: DbSession,
    unread_only: Annotated[
        bool, Query(description="Only notifications that have not been read.")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[NotificationRead]:
    """A page of the caller's own notifications, newest first.

    `unread_only` defaults to `False`, so the default call is the full history — the
    dropdown and the "all notifications" screen are the same request with a flag, and a
    client that forgets the flag gets the larger, more obviously-correct answer rather
    than an empty list.

    The unread badge does **not** come from here. It calls `/notifications/unread-count`,
    because counting a user's backlog by fetching and measuring it makes the cheapest
    endpoint in the feature the one whose cost grows fastest.
    """
    notifications = await notification_service.list_for_user(
        db, context, unread_only=unread_only, limit=limit, offset=offset
    )
    return [NotificationRead.model_validate(notification) for notification in notifications]


@router.get(
    "/notifications/unread-count",
    response_model=NotificationUnreadCount,
    summary="How many of the caller's notifications are unread",
    dependencies=_guard,
)
async def unread_count(context: Context, db: DbSession) -> NotificationUnreadCount:
    """The badge number. One `COUNT` against a partial index — see the repository."""
    return NotificationUnreadCount(unread=await notification_service.unread_count(db, context))


@router.post(
    "/notifications/read-all",
    response_model=NotificationMarkAllResult,
    summary="Mark every unread notification as read",
    dependencies=_guard,
)
async def mark_all_read(context: Context, db: DbSession) -> NotificationMarkAllResult:
    """Clear the badge in one statement.

    Idempotent, and it says so: the second call returns `marked_read: 0` because there is
    nothing left to change. The timestamp on a notification read last week is not moved,
    which is the property that makes "when did they read it" still answerable afterwards.
    """
    changed = await notification_service.mark_all_read(db, context)
    return NotificationMarkAllResult(marked_read=changed)


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationRead,
    summary="Mark one notification as read",
    dependencies=_guard,
)
async def mark_read(
    notification_id: uuid.UUID, context: Context, db: DbSession
) -> NotificationRead:
    """Mark one notification read, and return it.

    Returning the row rather than `204` so the client can render the result from the
    server's own state instead of assuming its optimistic update was right — and so a
    second call, which changes nothing, still returns the original `read_at`.

    A notification addressed to a colleague is `404` with the same body as one that does
    not exist (ADR-009).
    """
    notification = await notification_service.mark_read(db, context, notification_id)
    return NotificationRead.model_validate(notification)
