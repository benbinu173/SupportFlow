"""User administration endpoints.

Every route is guarded by a capability from `app/core/permissions.py` and scoped to
the caller's organization by the repository it builds. There is no route here that
takes an organization from the request, because there is no field for one.

`GET /{user_id}` requires `USER_LIST` rather than `PROFILE_VIEW`: it reads *any* user
in the tenant, whereas `PROFILE_VIEW` is the capability to read your own profile, and
that is served by `/auth/me`. A customer-role caller has no reason to fetch an
arbitrary user id.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Context, DbSession, Origin, require_permission
from app.core.permissions import Permission
from app.schemas.user import UserCreate, UserRead, UserRoleUpdate
from app.services import user_service

router = APIRouter()


@router.get(
    "",
    response_model=list[UserRead],
    summary="List users in the organization",
    dependencies=[Depends(require_permission(Permission.USER_LIST))],
)
async def list_users(
    context: Context,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[UserRead]:
    """A page of users in the caller's organization.

    Returns a plain array rather than a `{items, total}` envelope. Every field in an
    envelope has to be maintained and tested, and nothing consumes a total today —
    when a UI needs one, adding it is a compatible change.

    The ceiling on `limit` is enforced here rather than trusted from the client: an
    unbounded page size is a way to make the server do arbitrary work per request.
    """
    users = await user_service.list_users(db, context, limit=limit, offset=offset)
    return [UserRead.model_validate(user) for user in users]


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user in the organization",
    dependencies=[Depends(require_permission(Permission.USER_CREATE))],
)
async def create_user(
    payload: UserCreate, context: Context, db: DbSession, origin: Origin
) -> UserRead:
    """Add a user to the caller's organization.

    The new account's organization comes from the caller's verified tenant. The
    request body has no organization field, so creating a user in another tenant is
    not something a client can express.
    """
    user = await user_service.create_user(db, context, payload, origin=origin)
    return UserRead.model_validate(user)


@router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="Fetch one user",
    dependencies=[Depends(require_permission(Permission.USER_LIST))],
)
async def get_user(user_id: uuid.UUID, context: Context, db: DbSession) -> UserRead:
    """One user in the caller's organization.

    A `user_id` belonging to another tenant returns **404**, not 403 — indistinguishable
    from an id that does not exist. A 403 would confirm the record is real and merely
    out of reach, which is an enumeration oracle across tenants (ADR-009).
    """
    user = await user_service.get_user(db, context, user_id)
    return UserRead.model_validate(user)


@router.patch(
    "/{user_id}/role",
    response_model=UserRead,
    summary="Change a user's role",
    dependencies=[Depends(require_permission(Permission.USER_UPDATE_ROLE))],
)
async def update_role(
    user_id: uuid.UUID, payload: UserRoleUpdate, context: Context, db: DbSession, origin: Origin
) -> UserRead:
    """Change a user's role.

    Refused if it would leave the organization without an administrator — nobody would
    remain who could grant the role back.
    """
    user = await user_service.update_role(db, context, user_id, payload, origin=origin)
    return UserRead.model_validate(user)


@router.post(
    "/{user_id}/deactivate",
    response_model=UserRead,
    summary="Deactivate a user",
    dependencies=[Depends(require_permission(Permission.USER_DEACTIVATE))],
)
async def deactivate_user(
    user_id: uuid.UUID, context: Context, db: DbSession, origin: Origin
) -> UserRead:
    """Deactivate a user, preserving their history.

    Deactivation rather than deletion, so ticket authorship and audit records stay
    intact. Refused for your own account and for the last administrator.
    """
    user = await user_service.deactivate_user(db, context, user_id, origin=origin)
    return UserRead.model_validate(user)
