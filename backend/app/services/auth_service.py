"""Authentication: registration, login, refresh rotation, and logout.

The four operations that establish and end a session. Everything here is a plain
async function taking a session, so it can be exercised without an HTTP client.

Two rules run through the whole module:

* **A failure never says which part failed.** Unknown email, wrong password, expired
  token, replayed token — all of them produce the same error to the client. The
  difference is logged, never returned.
* **The database decides who you are.** Roles and `is_active` are read from the user
  row on every request, so a change takes effect on the next request rather than when
  a token expires (ADR-013).
"""

import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    AuthenticationRequiredError,
    ConflictError,
    InactiveUserError,
    InvalidCredentialsError,
)
from app.core.security import (
    create_access_token,
    dummy_verify,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    refresh_token_expiry,
    verify_and_update_password,
)
from app.models.enums import UserRole
from app.models.organization import Organization
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import find_users_by_email_across_tenants
from app.schemas.auth import LoginRequest, RegisterRequest

logger = structlog.get_logger(__name__)

# Longest slug the `slug` column accepts, before the uniqueness suffix.
_MAX_SLUG_LENGTH = 100


@dataclass(frozen=True, slots=True)
class AuthResult:
    """A freshly established session.

    `refresh_token` is the raw value and exists only between here and the response
    layer, which sets it as a cookie and discards it. It is never stored — the
    database holds a SHA-256 of it (ADR-003) — and must never be logged.
    """

    user: User
    access_token: str
    refresh_token: str
    expires_in: int


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _slugify(value: str) -> str:
    """Reduce a display name to a URL-safe slug.

    Unicode is folded to ASCII first, so "Ünïcode Ltd" becomes "unicode-ltd" rather
    than dropping those characters. Everything that is not a letter or digit collapses
    to a single hyphen, which handles spaces, punctuation, and the ampersands in
    company names under one rule.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    # A name made entirely of punctuation folds to nothing; "org" keeps the column
    # non-empty, and the uniqueness suffix below makes it distinguishable.
    return slug[:_MAX_SLUG_LENGTH] or "org"


async def _unique_slug(organizations: OrganizationRepository, name: str) -> str:
    """A slug derived from `name` that no organization is using.

    Checks once and, on collision, appends random hex rather than counting upwards.
    A counter (`acme-2`, `acme-3`) would disclose how many organizations share a name,
    which is exactly the kind of cross-tenant fact §11 keeps private.
    """
    base = _slugify(name)
    if not await organizations.slug_exists(base):
        return base
    return f"{base[: _MAX_SLUG_LENGTH - 7]}-{secrets.token_hex(3)}"


async def register(
    session: AsyncSession,
    payload: RegisterRequest,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> AuthResult:
    """Create an organization and its first administrator, then sign them in.

    Both records are written in one transaction: an organization with no admin is
    unreachable — nobody could ever create its first user — so it must not be possible
    to commit one without the other.

    Email uniqueness is per organization, not global, so the same person registering a
    second organization is not a conflict.
    """
    organizations = OrganizationRepository(session)
    slug = await _unique_slug(organizations, payload.organization_name)

    organization = Organization(name=payload.organization_name, slug=slug)
    organizations.add(organization)

    # flush, not commit: this assigns the server-generated UUID so the user row below
    # can reference it, while leaving the transaction open so both rows land or
    # neither does.
    await session.flush()

    user = User(
        organization_id=organization.id,
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        # The first account in a new tenant is necessarily its administrator. There
        # is no way to register into an existing organization.
        role=UserRole.ADMIN,
        last_login_at=datetime.now(UTC),
    )
    session.add(user)

    try:
        await session.flush()
    except IntegrityError as exc:
        # Two registrations picked the same slug in the same instant. The window is
        # milliseconds wide and the remedy is "try again", so this is reported rather
        # than retried — a retry would need a savepoint to recover the session, which
        # is more machinery than the race is worth.
        await session.rollback()
        raise ConflictError("That organization name is already taken.") from exc

    result, token_row = _issue_tokens(user, user_agent=user_agent, ip_address=ip_address)
    session.add(token_row)
    await session.commit()

    logger.info(
        "organization_registered",
        organization_id=str(organization.id),
        user_id=str(user.id),
    )
    return result


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def login(
    session: AsyncSession,
    payload: LoginRequest,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> AuthResult:
    """Verify credentials and start a session.

    An email can match more than one row — `(organization_id, email)` is the unique
    key, so the same person may hold accounts at two providers — so the password is
    tried against each candidate and the first match wins.

    A candidate whose password matches but whose account is deactivated does **not**
    end the search. The user may hold an active account elsewhere, and refusing them
    because an unrelated organization suspended them would be wrong. Only if no active
    candidate matches does the deactivation become the reported reason.
    """
    candidates = await find_users_by_email_across_tenants(session, payload.email)

    matched: User | None = None
    inactive_match = False
    updated_hash: str | None = None

    for candidate in candidates:
        valid, new_hash = verify_and_update_password(payload.password, candidate.password_hash)
        if not valid:
            continue
        if not candidate.is_active:
            inactive_match = True
            continue
        matched, updated_hash = candidate, new_hash
        break

    if matched is None:
        if not candidates:
            # No such email. Spend the same time a real check would spend, so response
            # latency does not reveal that this address is unknown.
            dummy_verify(payload.password)
        logger.info("login_failed", reason="deactivated" if inactive_match else "credentials")
        if inactive_match:
            raise InactiveUserError()
        raise InvalidCredentialsError()

    if updated_hash is not None:
        # The stored hash used older Argon2 parameters. This is the one moment the
        # plaintext exists, so take it.
        matched.password_hash = updated_hash

    matched.last_login_at = datetime.now(UTC)
    result, token_row = _issue_tokens(matched, user_agent=user_agent, ip_address=ip_address)
    session.add(token_row)
    await session.commit()

    logger.info(
        "login_succeeded",
        user_id=str(matched.id),
        organization_id=str(matched.organization_id),
    )
    return result


# ---------------------------------------------------------------------------
# Refresh and logout
# ---------------------------------------------------------------------------


async def refresh(
    session: AsyncSession,
    raw_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> AuthResult:
    """Exchange a refresh token for a new pair, rotating the old one out.

    Three outcomes, and the middle one is the security-relevant case:

    * **Unknown token** — 401. Nothing to revoke, nothing learned.
    * **Already-used token** — a replay. Either an attacker holding a stolen token or
      the legitimate user re-sending a request; both get the same response, and every
      live session for that user is revoked (see `revoke_all_for_user`).
    * **Expired token** — 401, and the row is closed out so it cannot be presented
      again.

    The row lock taken in `find_by_hash` is what makes this safe under concurrency:
    two simultaneous refreshes of one token would otherwise both read an unrevoked row
    and both mint a session, and neither would see a replay.
    """
    tokens = RefreshTokenRepository(session)
    token = await tokens.find_by_hash(hash_refresh_token(raw_token))

    if token is None:
        logger.warning("refresh_token_unknown")
        raise AuthenticationRequiredError()

    if token.revoked_at is not None:
        logger.warning(
            "refresh_token_reuse_detected",
            user_id=str(token.user_id),
            organization_id=str(token.organization_id),
            detail="revoking every live session for this user",
        )
        await tokens.revoke_all_for_user(token.user_id)
        await session.commit()
        raise AuthenticationRequiredError()

    if token.expires_at <= datetime.now(UTC):
        logger.info("refresh_token_expired", user_id=str(token.user_id))
        await tokens.revoke(token)
        await session.commit()
        raise AuthenticationRequiredError()

    user = await session.get(User, token.user_id)

    if user is None:
        # The account was deleted while a token was still outstanding. Nothing to
        # authenticate; still revoke the family so the row cannot be retried.
        await tokens.revoke_all_for_user(token.user_id)
        await session.commit()
        raise AuthenticationRequiredError()

    if not user.is_active:
        # Deactivated between issuing the token and using it. Without this check a
        # deactivated user could keep refreshing indefinitely.
        await tokens.revoke_all_for_user(token.user_id)
        await session.commit()
        logger.info("refresh_rejected_inactive_user", user_id=str(user.id))
        raise InactiveUserError()

    result, replacement = _issue_tokens(user, user_agent=user_agent, ip_address=ip_address)

    await tokens.revoke(token)
    session.add(replacement)
    # flush so the replacement has an id to link the old row to.
    await session.flush()
    token.replaced_by_id = replacement.id
    await session.commit()

    logger.info(
        "refresh_token_rotated",
        user_id=str(user.id),
        organization_id=str(user.organization_id),
    )
    return result


async def logout(session: AsyncSession, raw_token: str | None) -> None:
    """Revoke the presented refresh token.

    Idempotent, and silent when the token is unknown or already revoked. A logout that
    reported "that token does not exist" would be a free oracle for testing whether a
    stolen token is still live, and there is nothing useful for the client to do with
    the difference anyway.

    Only the presented session ends. Ending every session is a separate intent, and
    `revoke_all_for_user` already exists for the reuse-detection path.
    """
    if not raw_token:
        return

    tokens = RefreshTokenRepository(session)
    token = await tokens.find_by_hash(hash_refresh_token(raw_token), for_update=False)
    if token is None or token.revoked_at is not None:
        return

    await tokens.revoke(token)
    await session.commit()
    logger.info("logout", user_id=str(token.user_id))


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------


def _issue_tokens(
    user: User,
    *,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[AuthResult, RefreshToken]:
    """Mint an access token and the matching refresh-token row.

    Returns the row rather than adding it, so issuance can happen wherever the caller
    is in its transaction and the caller keeps control of the commit. Both callers add
    it immediately; keeping that explicit is what stops a future one from minting a
    token that never reaches the database.
    """
    settings = get_settings()

    raw_refresh = generate_refresh_token()
    row = RefreshToken(
        organization_id=user.organization_id,
        user_id=user.id,
        # Only the digest is stored. A database read must not yield a usable
        # credential (ADR-003).
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=refresh_token_expiry(),
        # Recorded for post-incident investigation only. Both are client-controlled
        # and trivially forged, so neither is ever an authorization input.
        user_agent=user_agent,
        ip_address=ip_address,
    )

    result = AuthResult(
        user=user,
        access_token=create_access_token(user.id, user.organization_id, user.role),
        refresh_token=raw_refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return result, row
