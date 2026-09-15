"""Request dependencies: authentication, tenant context, and permission guards.

This module is where the project's central security claim is actually made. Spec §4:
*"Never trust organization_id from the frontend. Derive it from the authenticated
user."* `get_tenant_context` is the only place a `TenantContext` is constructed, and
every element of it comes from a verified token and the database row that token names.

The order of checks matters and is deliberate:

1. **Token verifies.** Otherwise there is no identity to reason about.
2. **User exists.** A token for a deleted user is not a token.
3. **User is active.** Checked on *every* request, not only at login, so deactivation
   takes effect immediately rather than up to 15 minutes later (ADR-013).
4. **The token's tenant matches the user's.** A disagreement means the token was
   minted for a different tenant and then presented against this one.
5. **The organization is active.** A suspended tenant stops being served.

Only then is a `TenantContext` built.
"""

from collections.abc import Coroutine
from typing import Annotated, Any, Protocol, cast

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.database import get_db
from app.core.exceptions import (
    AuthenticationRequiredError,
    InactiveUserError,
    PermissionDeniedError,
    TenantAccessDeniedError,
)
from app.core.permissions import Permission
from app.core.security import decode_access_token
from app.core.tenancy import RequestOrigin, TenantContext
from app.models.enums import OrganizationStatus
from app.models.user import User

logger = structlog.get_logger(__name__)

# `auto_error=False` so a missing header produces our §42 envelope rather than
# FastAPI's own `{"detail": "Not authenticated"}`. Two error shapes in one API is a
# small thing that makes every client's error handling worse.
_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")

Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(credentials: Credentials, db: DbSession) -> User:
    """The authenticated user, loaded fresh from the database.

    The access token is treated as an *identifier*, never as a record of facts. What
    it says about the user's role and tenant is checked against the row, and the row
    wins. A stateless token that carried authority would keep asserting a revoked role
    until it expired.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationRequiredError()

    claims = decode_access_token(credentials.credentials)

    # `joinedload` rather than two round trips: the organization is needed below for
    # its status, and lazily touching `user.organization` in async code raises.
    result = await db.execute(
        select(User).where(User.id == claims.user_id).options(joinedload(User.organization))
    )
    user = result.scalar_one_or_none()

    if user is None:
        logger.warning("token_for_unknown_user", user_id=str(claims.user_id))
        raise AuthenticationRequiredError()

    if not user.is_active:
        raise InactiveUserError()

    if user.organization_id != claims.organization_id:
        # The token names a different tenant than the user actually belongs to. Not a
        # routine cross-tenant read — that returns 404 elsewhere. This is a token that
        # should not exist, so it is logged loudly and refused.
        logger.error(
            "token_tenant_mismatch",
            user_id=str(user.id),
            token_organization_id=str(claims.organization_id),
            user_organization_id=str(user.organization_id),
        )
        raise TenantAccessDeniedError()

    organization = user.organization
    if organization is None or organization.status is not OrganizationStatus.ACTIVE:
        logger.info("organization_not_active", organization_id=str(user.organization_id))
        raise TenantAccessDeniedError("This organization is not active.")

    return user


async def get_tenant_context(
    user: Annotated[User, Depends(get_current_user)],
) -> TenantContext:
    """The tenant identity for this request.

    Built from the database row, never from a header, body field, or query parameter.
    A request cannot influence which organization it is served as.
    """
    return TenantContext(
        user_id=user.id,
        organization_id=user.organization_id,
        # From the row, not from `claims.role`. See the module docstring.
        role=user.role,
        # Also from the row. `NULL` for staff, and for a portal account that was never
        # linked to a customer — the repository reads that as "reaches no rows", not
        # as "reaches every row" (ADR-015).
        customer_id=user.customer_id,
        # For the audit log's denormalized actor column, not for any decision. See the
        # field's comment on `TenantContext`.
        email=user.email,
    )


CurrentUser = Annotated[User, Depends(get_current_user)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


class PermissionGuard(Protocol):
    """A dependency built by `require_permission`.

    Exists so the capability a guard enforces can be *read* and not only executed.
    `tests/security/test_route_protection.py` walks the routing table and asserts every
    protected route declares one, which is the check that catches a new endpoint added
    without a capability.
    """

    requires: tuple[Permission, ...]

    def __call__(self, context: TenantContext) -> Coroutine[Any, Any, TenantContext]: ...


def require_permission(*permissions: Permission) -> PermissionGuard:
    """Build a dependency that requires **all** of `permissions`.

    Used as `dependencies=[Depends(require_permission(Permission.USER_CREATE))]` on a
    route. Centralising the check here is what Phase G means by "no scattered role
    comparisons": a route states the capability it needs and never inspects a role.
    """

    async def dependency(context: Context) -> TenantContext:
        if not context.has(*permissions):
            logger.info(
                "permission_denied",
                user_id=str(context.user_id),
                organization_id=str(context.organization_id),
                role=context.role.value,
                required=[str(permission) for permission in permissions],
            )
            raise PermissionDeniedError()
        return context

    # Carried on the closure so a route's requirement is introspectable. Nothing reads
    # this at request time; it is evidence for the authorization audit.
    #
    # The cast is because the attribute is established on the next line rather than in
    # the function's own definition — mypy checks the protocol at the assignment, and at
    # that point the object genuinely does not satisfy it yet.
    guard = cast("PermissionGuard", dependency)
    guard.requires = permissions
    return guard


def client_ip(request: Request) -> str:
    """The client address, for rate-limit keying.

    Deliberately **not** `X-Forwarded-For`. No trusted proxy is configured, so that
    header is set by the client and would let a single attacker rotate it freely and
    bypass the limit entirely — the header is a hint, and a limiter keyed on a
    client-controlled value does nothing. `request.client` is the peer address as
    reported by the socket.

    Behind a real reverse proxy this needs revisiting: every request would appear to
    come from the proxy, collapsing the limiter to a single global bucket. The fix is
    a configured trusted-proxy list, not blindly trusting the header.
    """
    return request.client.host if request.client else "unknown"


def header_or_none(request: Request, name: str, *, max_length: int) -> str | None:
    """Read a request header, truncated to a column width.

    Used for the two refresh-token provenance fields, both of which are client-supplied
    and stored for investigation only — never for authorization.
    """
    value = request.headers.get(name)
    return value[:max_length] if value else None


def request_origin(request: Request) -> RequestOrigin:
    """Build the `RequestOrigin` for this request.

    The two fields come from the two existing helpers rather than reading the request
    directly: `client_ip` carries the reasoning about why `X-Forwarded-For` is not
    consulted, and `header_or_none` carries the truncation rule. Neither is worth
    restating here, where a divergence would be invisible.
    """
    return RequestOrigin(
        ip_address=client_ip(request),
        # 500 matches the column width on `audit_logs.user_agent`; the refresh-token
        # provenance fields truncate for the same reason. A client can send a header of
        # any length it likes, and an over-long one must not become an integrity error.
        user_agent=header_or_none(request, "user-agent", max_length=500),
    )


Origin = Annotated[RequestOrigin, Depends(request_origin)]
