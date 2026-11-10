"""Cryptographic primitives: password hashing, access tokens, refresh tokens.

Three unrelated mechanisms live here because they share one property: each is a place
where a subtle mistake is invisible in testing and catastrophic in production. Keeping
them together means there is exactly one module to audit.

| Concern | Mechanism | Decision |
|---|---|---|
| Password storage | Argon2id via `pwdlib` | ADR-001 |
| Access token | JWT (HS256) via `PyJWT` | ADR-002 |
| Refresh token | Opaque random string, SHA-256 at rest | ADR-003 |

Nothing here touches the database or the request. Callers pass plain values in and get
plain values out, which is what makes all three testable without a fixture.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Final

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.core.config import get_settings
from app.core.exceptions import AuthenticationRequiredError
from app.models.enums import UserRole

# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

# `recommended()` is Argon2id with pwdlib's current tuned parameters, rather than
# hand-picked constants. Upgrading the parameters later is a library bump, and
# `needs_rehash` lets old hashes be upgraded on next successful login.
_password_hash: Final[PasswordHash] = PasswordHash.recommended()


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage."""
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored hash.

    Returns `False` rather than raising on a malformed hash. An unreadable hash means
    a corrupted row, and the correct response to that is "these credentials do not
    work" — not a 500 that confirms the account exists.
    """
    try:
        return _password_hash.verify(password, password_hash)
    except UnknownHashError:
        return False


def verify_and_update_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Check a password, and report a replacement hash if the stored one is stale.

    Returns `(is_valid, new_hash)`, where `new_hash` is `None` unless the stored hash
    was produced with older Argon2 parameters. The login service persists `new_hash`
    when it is set, so hashes upgrade themselves on next login as the library's tuned
    parameters change — no migration, no forced password reset.

    Verification and the staleness check are one call because pwdlib computes them
    together; calling `verify` and a separate rehash check would derive the password
    twice.
    """
    try:
        return _password_hash.verify_and_update(password, password_hash)
    except UnknownHashError:
        # A malformed hash means a corrupted row. "These credentials do not work" is
        # the right answer; a 500 would confirm the account exists.
        return False, None


@lru_cache
def _dummy_hash() -> str:
    """A real Argon2 hash of a random string, used to burn equivalent CPU.

    Computed once on first use rather than at import: it costs a full Argon2
    derivation, and paying that at startup for something most requests never touch
    would be waste.

    The value is meaningless as a credential — it hashes a value nobody knows, and
    no account is created with it.
    """
    return hash_password(secrets.token_urlsafe(32))


def dummy_verify(password: str) -> None:
    """Spend the same time a real password check would, then discard the result.

    Called by the login service when the email does not match any user. Without it,
    "unknown email" returns in microseconds while "wrong password" takes ~50ms of
    Argon2, and that difference is a reliable account-enumeration oracle — which is
    exactly what returning an identical error message was meant to prevent.

    Returns nothing on purpose: the caller must not branch on the outcome.
    """
    verify_password(password, _dummy_hash())


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------

# PyJWT refuses a non-string `sub` (it is a registered claim with a registered type),
# so user ids travel as strings and are parsed back on the way in. There is no way to
# disable that check short of a deprecated option, and it is a good check.
_SUBJECT: Final = "sub"
_ORGANIZATION: Final = "org"
_ROLE: Final = "role"
_VERSION: Final = "ver"
_RESET_AUDIENCE: Final = "supportflow:password-reset"
_VERIFICATION_AUDIENCE: Final = "supportflow:email-verification"


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """The verified contents of an access token.

    Deliberately not the authorization decision. `role` here is what the token was
    minted with — an assertion, possibly up to `ACCESS_TOKEN_EXPIRE_MINUTES` stale.
    `app/api/deps.py` treats the *database* role as authoritative and uses this only
    as a cross-check (ADR-013).
    """

    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: UserRole
    token_version: int = 0


def create_access_token(
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role: UserRole,
    *,
    expires_delta: timedelta | None = None,
    token_version: int = 0,
) -> str:
    """Mint a signed access token.

    The tenant id is embedded so a token cannot be replayed against another
    organization even if a user row somehow moved between tenants; `deps.py` asserts
    the claim still matches the row it loads.
    """
    settings = get_settings()
    issued_at = datetime.now(UTC)
    expires_at = issued_at + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    payload: dict[str, Any] = {
        _SUBJECT: str(user_id),
        _ORGANIZATION: str(organization_id),
        _ROLE: role.value,
        _VERSION: token_version,
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> AccessTokenClaims:
    """Verify a token and return its claims, or raise `AuthenticationRequiredError`.

    One exception type for expired, tampered, malformed, and wrong-secret tokens. The
    client cannot act differently on any of them — every case means "log in again" —
    and telling them apart would tell an attacker which half of the problem to work
    on. The PyJWT exception is chained as `__cause__` so the log can still record the
    real reason.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            # Pinned rather than read from the token's own header. A decoder that
            # honours `alg: none` — or an asymmetric algorithm with the public key as
            # the HMAC secret — is the classic JWT forgery.
            algorithms=[settings.JWT_ALGORITHM],
            # `exp` is required, not optional: a token with no expiry would be a
            # permanent credential.
            options={"require": ["exp", "sub", _ORGANIZATION]},
        )
        return AccessTokenClaims(
            user_id=uuid.UUID(payload[_SUBJECT]),
            organization_id=uuid.UUID(payload[_ORGANIZATION]),
            role=UserRole(payload[_ROLE]),
            token_version=_token_version(payload),
        )
    # ValueError covers the UUID and enum parses above, plus PyJWT's own subclassing;
    # listing the concrete types would be a list to keep in sync with a library.
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise AuthenticationRequiredError() from exc


def _token_version(payload: dict[str, Any]) -> int:
    # Older access tokens predate this claim and remain valid until their first reset.
    value = payload.get(_VERSION, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid token version")
    return value


@dataclass(frozen=True, slots=True)
class PasswordResetClaims:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    token_version: int


def _password_reset_key() -> bytes:
    """Domain separation prevents a recovery token from authenticating as an access token."""
    return hmac.digest(get_settings().JWT_SECRET.encode(), _RESET_AUDIENCE.encode(), "sha256")


def create_password_reset_token(
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    token_version: int,
    *,
    expires_at: datetime,
) -> str:
    """Create a short-lived link; its account version makes it single-use after reset."""
    settings = get_settings()
    return jwt.encode(
        {
            "sub": str(user_id),
            "org": str(organization_id),
            "ver": token_version,
            "aud": _RESET_AUDIENCE,
            "iat": datetime.now(UTC),
            "exp": expires_at,
            "jti": secrets.token_urlsafe(16),
        },
        _password_reset_key(),
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_password_reset_token(token: str) -> PasswordResetClaims:
    from app.core.exceptions import InvalidResetTokenError

    try:
        payload = jwt.decode(
            token,
            _password_reset_key(),
            algorithms=[get_settings().JWT_ALGORITHM],
            audience=_RESET_AUDIENCE,
            options={"require": ["exp", "sub", "org", "ver", "aud"]},
        )
        if not isinstance(payload["sub"], str) or not isinstance(payload["org"], str):
            raise ValueError("invalid reset identity")
        return PasswordResetClaims(
            user_id=uuid.UUID(payload["sub"]),
            organization_id=uuid.UUID(payload["org"]),
            token_version=_token_version(payload),
        )
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise InvalidResetTokenError() from exc


@dataclass(frozen=True, slots=True)
class EmailVerificationClaims:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    email_hash: str


def _email_verification_key() -> bytes:
    return hmac.digest(
        get_settings().JWT_SECRET.encode(), _VERIFICATION_AUDIENCE.encode(), "sha256"
    )


def email_verification_fingerprint(email: str) -> str:
    """Bind a link and queued task to an address without copying that address to Redis."""
    return hmac.digest(_email_verification_key(), email.encode(), "sha256").hex()


def create_email_verification_token(
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    email_hash: str,
    *,
    expires_at: datetime,
) -> str:
    """This signature and audience are independent of access and recovery credentials."""
    return jwt.encode(
        {
            "sub": str(user_id),
            "org": str(organization_id),
            "email_hash": email_hash,
            "aud": _VERIFICATION_AUDIENCE,
            "iat": datetime.now(UTC),
            "exp": expires_at,
            "jti": secrets.token_urlsafe(16),
        },
        _email_verification_key(),
        algorithm=get_settings().JWT_ALGORITHM,
    )


def decode_email_verification_token(token: str) -> EmailVerificationClaims:
    from app.core.exceptions import InvalidVerificationTokenError

    try:
        payload = jwt.decode(
            token,
            _email_verification_key(),
            algorithms=[get_settings().JWT_ALGORITHM],
            audience=_VERIFICATION_AUDIENCE,
            options={"require": ["exp", "sub", "org", "email_hash", "aud"]},
        )
        if not isinstance(payload["sub"], str) or not isinstance(payload["org"], str):
            raise ValueError("invalid verification identity")
        email_hash = payload["email_hash"]
        if (
            not isinstance(email_hash, str)
            or len(email_hash) != 64
            or any(character not in "0123456789abcdef" for character in email_hash)
        ):
            raise ValueError("invalid verification address")
        return EmailVerificationClaims(
            user_id=uuid.UUID(payload["sub"]),
            organization_id=uuid.UUID(payload["org"]),
            email_hash=email_hash,
        )
    except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError, KeyError) as exc:
        raise InvalidVerificationTokenError() from exc


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------

# 32 bytes → 43 URL-safe characters, ~256 bits of entropy. Per ADR-003 the token is
# opaque rather than a JWT: it must be revocable server-side, and a self-describing
# token cannot be.
_REFRESH_TOKEN_BYTES: Final = 32


def generate_refresh_token() -> str:
    """Mint a new refresh token. Returned to the client once and never stored."""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """Hash a refresh token for storage.

    SHA-256, not Argon2. The usual reason for a slow hash is that human passwords are
    guessable; a 256-bit random string is not, so there is nothing for slowness to
    defend against. What matters is only that a database read yields no usable
    credential — which a plain digest achieves. Deterministic by necessity: the lookup
    is `WHERE token_hash = :hash`, so the hash must be stable, and a salted scheme
    would make that query impossible.

    64 hex characters, matching the `token_hash` column width.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_token_expiry() -> datetime:
    """When a token minted now should stop being accepted."""
    return datetime.now(UTC) + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS)


def refresh_token_ttl_seconds() -> int:
    """The refresh token lifetime in seconds, for cookie `Max-Age`."""
    return get_settings().REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
