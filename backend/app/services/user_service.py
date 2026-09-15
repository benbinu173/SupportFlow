"""User management within a tenant.

Every function takes a `TenantContext` and builds a repository from it, so the
organization filter is never a parameter a caller could get wrong. A user id naming
another tenant's row produces `USER_NOT_FOUND` and a 404, identical to a genuinely
absent id (ADR-009).

The capability check itself is not here — that happens at the route, via
`require_permission`. This layer enforces the rules that a capability alone cannot
express: the invariants that must hold whatever the caller is allowed to do.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ErrorCode,
    NotFoundError,
    UserAlreadyExistsError,
    ValidationError,
)
from app.core.permissions import PORTAL_ROLES
from app.core.security import hash_password
from app.core.tenancy import RequestOrigin, TenantContext
from app.models.enums import AuditAction, UserRole
from app.models.user import User
from app.repositories.customer_repository import CustomerRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserCreate, UserRoleUpdate
from app.services import audit_service

logger = structlog.get_logger(__name__)


async def list_users(
    session: AsyncSession, context: TenantContext, *, limit: int, offset: int
) -> list[User]:
    """A page of users in the caller's organization."""
    return list(await UserRepository(session, context).list_users(limit=limit, offset=offset))


async def get_user(session: AsyncSession, context: TenantContext, user_id: uuid.UUID) -> User:
    """One user, or 404 — including when the id belongs to another organization."""
    user = await UserRepository(session, context).get(user_id)
    if user is None:
        raise NotFoundError(ErrorCode.USER_NOT_FOUND)
    return user


async def create_user(
    session: AsyncSession,
    context: TenantContext,
    payload: UserCreate,
    *,
    origin: RequestOrigin | None = None,
) -> User:
    """Add a user to the caller's organization.

    The new user's `organization_id` comes from `context` and from nowhere else. The
    request body has no field for it, so a caller cannot even attempt to create a user
    in someone else's tenant.

    `customer_id` is the one conditional field: a portal account must name the customer
    it acts as, and a staff account must not. The link is what makes `RowScope.OWN`
    resolvable — a portal account without it authenticates successfully and then sees
    nothing — so requiring it here is what stops that state being reachable through the
    API at all. `PORTAL_ROLES` decides which roles these are, rather than a role
    comparison in this file; see the note on centralization in `app/core/permissions.py`.
    """
    repository = UserRepository(session, context)
    is_portal = payload.role in PORTAL_ROLES

    if is_portal and payload.customer_id is None:
        raise ValidationError(
            "A customer account must be linked to a customer record. "
            "Pass customer_id, or create the customer first."
        )
    if not is_portal and payload.customer_id is not None:
        raise ValidationError("Only a customer account can be linked to a customer record.")

    if payload.customer_id is not None:
        # Tenant-scoped, so a customer id from another organization resolves to
        # nothing and is reported as absent rather than refused as forbidden (ADR-009).
        customer = await CustomerRepository(session, context).get(payload.customer_id)
        if customer is None:
            raise NotFoundError(ErrorCode.CUSTOMER_NOT_FOUND)

    if await repository.email_taken(payload.email):
        raise UserAlreadyExistsError()

    user = User(
        organization_id=context.organization_id,
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        customer_id=payload.customer_id,
    )
    repository.add(user)
    # Flushed, not committed, so the id exists for the audit row and both land in one
    # transaction. §34 asks for `USER_CREATED`; a user that exists without it would be
    # an account nobody can account for.
    await session.flush()
    audit_service.record_for(
        session,
        context,
        AuditAction.USER_CREATED,
        # The new user is the target; the caller is the actor. Two different rows, and
        # conflating them would make "who created this account" unanswerable.
        target_type="user",
        target_id=user.id,
        after={"role": payload.role.value, "email": payload.email},
        origin=origin,
    )
    await session.commit()

    logger.info(
        "user_created",
        user_id=str(user.id),
        organization_id=str(context.organization_id),
        role=payload.role.value,
        actor_id=str(context.user_id),
    )
    return user


async def update_role(
    session: AsyncSession,
    context: TenantContext,
    user_id: uuid.UUID,
    payload: UserRoleUpdate,
    *,
    origin: RequestOrigin | None = None,
) -> User:
    """Change a user's role.

    Refuses to remove the last administrator. An organization with no admin has nobody
    who can create users, reassign roles, or configure anything — and no way back
    short of an edit to the database.
    """
    repository = UserRepository(session, context)

    user = await repository.get(user_id)
    if user is None:
        raise NotFoundError(ErrorCode.USER_NOT_FOUND)

    if user.role is UserRole.ADMIN and payload.role is not UserRole.ADMIN:
        await _require_another_admin(repository, user, action="change this user's role")

    previous = user.role
    # A no-op role change writes no audit row. §34 records *actions*, and an entry
    # saying a role changed from admin to admin records an event that did not happen —
    # which is worse than a gap, because it is wrong rather than absent.
    if previous is payload.role:
        return user

    user.role = payload.role
    audit_service.record_for(
        session,
        context,
        AuditAction.USER_ROLE_UPDATED,
        target_type="user",
        target_id=user.id,
        # Both ends, per §34's "before/after values where appropriate". This is the one
        # action where the pair is the whole point of the record.
        before={"role": previous.value},
        after={"role": payload.role.value},
        origin=origin,
    )
    await session.commit()

    logger.info(
        "user_role_updated",
        user_id=str(user.id),
        organization_id=str(context.organization_id),
        role=payload.role.value,
        actor_id=str(context.user_id),
    )
    return user


async def deactivate_user(
    session: AsyncSession,
    context: TenantContext,
    user_id: uuid.UUID,
    *,
    origin: RequestOrigin | None = None,
) -> User:
    """Deactivate a user.

    Deactivation rather than deletion, so audit history and ticket authorship stay
    intact. Two refusals:

    * **Yourself.** A deactivated user fails the `is_active` check on the very next
      request, so self-deactivation ends the current session immediately and leaves no
      way to undo it. Use another admin, or another session.
    * **The last administrator.** Same reasoning as `update_role`.
    """
    repository = UserRepository(session, context)

    user = await repository.get(user_id)
    if user is None:
        raise NotFoundError(ErrorCode.USER_NOT_FOUND)

    if user.id == context.user_id:
        raise ValidationError("You cannot deactivate your own account.")

    if user.role is UserRole.ADMIN:
        await _require_another_admin(repository, user, action="deactivate this user")

    user.is_active = False
    audit_service.record_for(
        session,
        context,
        AuditAction.USER_DEACTIVATED,
        target_type="user",
        target_id=user.id,
        before={"is_active": True},
        after={"is_active": False},
        origin=origin,
    )
    await session.commit()

    logger.info(
        "user_deactivated",
        user_id=str(user.id),
        organization_id=str(context.organization_id),
        actor_id=str(context.user_id),
    )
    return user


async def _require_another_admin(repository: UserRepository, user: User, *, action: str) -> None:
    """Raise unless some administrator other than `user` remains.

    Counts rather than inspecting the caller's own role: the caller might be a second
    admin acting on the first, in which case the operation is legitimate. What matters
    is only whether the organization would be left with zero.
    """
    remaining = await repository.count_admins(excluding=user.id)
    if remaining == 0:
        raise ValidationError(
            f"Cannot {action}: this is the organization's only administrator. "
            "Promote another user to admin first."
        )
