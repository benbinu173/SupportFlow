"""Authentication endpoints (spec §36).

Five routes: register, login, refresh, logout, and the caller's own profile.

The refresh token never appears in a response body. It is set as an `HttpOnly` cookie
scoped to this router's path, which is simultaneously the storage decision and the CSRF
control: `SameSite=Lax` means a cross-site POST does not carry it, so the refresh
endpoint cannot be driven by a page the user did not intend to visit (ADR-014).

`/logout` is the one route here that requires a valid access token — see the note on
the route itself.
"""

import structlog
from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import (
    Context,
    CurrentUser,
    DbSession,
    client_ip,
    header_or_none,
    require_permission,
)
from app.api.rate_limits import limit_login, limit_register
from app.core.config import get_settings
from app.core.exceptions import AuthenticationRequiredError
from app.core.permissions import Permission
from app.core.security import refresh_token_ttl_seconds
from app.schemas.auth import LoginRequest, LogoutResponse, RegisterRequest, TokenResponse
from app.schemas.user import UserRead
from app.services import auth_service

logger = structlog.get_logger(__name__)

router = APIRouter()

# Width of the `refresh_tokens.user_agent` column.
_MAX_USER_AGENT = 500


# ---------------------------------------------------------------------------
# Cookie handling
# ---------------------------------------------------------------------------


def _set_refresh_cookie(response: Response, token: str) -> None:
    """Attach the refresh token as a cookie.

    Every attribute is load-bearing:

    * `httponly` — JavaScript cannot read it, so an XSS flaw cannot exfiltrate a
      long-lived credential the way it could steal one from `localStorage`.
    * `samesite="lax"` — the CSRF control. A cross-site POST does not carry the
      cookie, so no token-pair dance is needed. Development still works:
      `localhost:5173` and `localhost:8000` are the same *site* — the port is not part
      of a site — so Lax cookies are sent between them.
    * `secure` — off in development only, where the app is served over plain HTTP and
      a `Secure` cookie is silently dropped by the browser. That failure presents as
      "refresh randomly does not work", not as a configuration error.
    * `path` — scoped to `/api/v1/auth`, so a long-lived credential is not attached to
      every request the API serves, only to the endpoints that can use it.
    """
    settings = get_settings()
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
        path=settings.refresh_cookie_path,
        max_age=refresh_token_ttl_seconds(),
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Remove the cookie.

    The attributes must match those used to set it. A mismatch on `path` (or on
    `secure`) makes the browser treat this as a different cookie, and the original
    survives logout.
    """
    settings = get_settings()
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
        path=settings.refresh_cookie_path,
    )


def _refresh_cookie_value(request: Request) -> str | None:
    """Read the refresh cookie, by the configured name rather than a literal."""
    return request.cookies.get(get_settings().REFRESH_COOKIE_NAME)


def _user_agent(request: Request) -> str | None:
    """The User-Agent header, truncated to the column width.

    Recorded against a refresh token for post-incident investigation only. It is
    client-supplied and trivially forged, so it is never an authorization input.
    """
    return header_or_none(request, "user-agent", max_length=_MAX_USER_AGENT)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an organization and its first administrator",
    dependencies=[Depends(limit_register)],
)
async def register(
    payload: RegisterRequest, request: Request, response: Response, db: DbSession
) -> TokenResponse:
    """Register a new tenant.

    201 rather than 200: an organization and a user were created. Registration signs
    the new administrator in directly — forcing a second round trip to `/login` with
    credentials the client already holds would be ceremony, and it is not what any
    SaaS signup does.
    """
    result = await auth_service.register(
        db, payload, user_agent=_user_agent(request), ip_address=client_ip(request)
    )
    _set_refresh_cookie(response, result.refresh_token)
    return TokenResponse(access_token=result.access_token, expires_in=result.expires_in)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for an access token",
    dependencies=[Depends(limit_login)],
)
async def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession
) -> TokenResponse:
    """Authenticate and start a session."""
    result = await auth_service.login(
        db, payload, user_agent=_user_agent(request), ip_address=client_ip(request)
    )
    _set_refresh_cookie(response, result.refresh_token)
    return TokenResponse(access_token=result.access_token, expires_in=result.expires_in)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the refresh token and issue a new access token",
)
async def refresh(request: Request, response: Response, db: DbSession) -> TokenResponse:
    """Rotate a session.

    Unauthenticated by bearer token, necessarily: the point is to obtain a new access
    token after the current one expired. The refresh cookie is the credential, and it
    is a rotating one — each success invalidates the token presented and issues a
    replacement, so a stolen token is usable at most once and its use is detected
    (ADR-003).

    Not rate-limited by IP: the credential is a 256-bit random value, so there is
    nothing to guess, and keying on IP would throttle a shared office NAT for no gain.
    """
    raw_token = _refresh_cookie_value(request)
    if not raw_token:
        raise AuthenticationRequiredError()

    result = await auth_service.refresh(
        db, raw_token, user_agent=_user_agent(request), ip_address=client_ip(request)
    )
    _set_refresh_cookie(response, result.refresh_token)
    return TokenResponse(access_token=result.access_token, expires_in=result.expires_in)


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="End the current session",
)
async def logout(
    request: Request, response: Response, db: DbSession, _context: Context
) -> LogoutResponse:
    """Revoke the presented refresh token and clear the cookie.

    Unlike its siblings this route **requires** a valid access token. That preserves
    the invariant a test enforces mechanically — every non-public route is
    authenticated — and a client whose access token has expired can call `/refresh`
    first, which is a normal flow rather than a workaround.

    Idempotent: an unknown, expired, or already-revoked cookie succeeds silently. A
    route that reported "that token does not exist" would be a free oracle for probing
    whether a stolen token is still live, and there is nothing useful for the client
    to do with the difference anyway.
    """
    await auth_service.logout(db, _refresh_cookie_value(request))
    _clear_refresh_cookie(response)
    return LogoutResponse()


@router.get(
    "/me",
    response_model=UserRead,
    summary="The authenticated user",
    dependencies=[Depends(require_permission(Permission.PROFILE_VIEW))],
)
async def me(user: CurrentUser) -> UserRead:
    """Who the caller is, according to the verified token and the database.

    Useful as a session check from the frontend: a 200 confirms the token is live, and
    the body reports which role and tenant the caller is being served as.
    """
    return UserRead.model_validate(user)
