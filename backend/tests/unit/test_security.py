"""Cryptographic primitives: password hashing, JWTs, refresh tokens.

Unit tests — no database, no HTTP. These are the functions where a mistake is
invisible in normal use and total in effect, so they are worth exercising directly
rather than only through the endpoints that call them.
"""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.exceptions import AuthenticationRequiredError
from app.core.security import (
    create_access_token,
    decode_access_token,
    dummy_verify,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    refresh_token_expiry,
    refresh_token_ttl_seconds,
    verify_and_update_password,
    verify_password,
)
from app.models.enums import UserRole

pytestmark = pytest.mark.unit

USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ORG_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_a_password_verifies_against_its_own_hash() -> None:
    stored = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", stored)


def test_a_wrong_password_does_not_verify() -> None:
    stored = hash_password("correct-horse-battery-staple")
    assert not verify_password("Correct-horse-battery-staple", stored)
    assert not verify_password("", stored)


def test_the_plaintext_is_not_recoverable_from_the_hash() -> None:
    password = "correct-horse-battery-staple"
    stored = hash_password(password)

    assert password not in stored
    # Argon2id's encoded form, which records the algorithm and its parameters.
    assert stored.startswith("$argon2id$")


def test_the_same_password_hashes_differently_every_time() -> None:
    """A per-hash salt, without which identical passwords would be visibly identical.

    This is what stops a database dump from revealing that two users share a password
    — and what makes a precomputed table useless.
    """
    first = hash_password("correct-horse-battery-staple")
    second = hash_password("correct-horse-battery-staple")

    assert first != second
    # Both still verify, so the difference is the salt and not a broken round trip.
    assert verify_password("correct-horse-battery-staple", first)
    assert verify_password("correct-horse-battery-staple", second)


def test_a_current_hash_needs_no_update() -> None:
    """`verify_and_update` reports a replacement only when the parameters changed."""
    password = "correct-horse-battery-staple"
    valid, updated = verify_and_update_password(password, hash_password(password))

    assert valid is True
    assert updated is None


def test_a_malformed_hash_fails_closed_rather_than_raising() -> None:
    """A corrupted row must read as "these credentials do not work".

    Raising instead would produce a 500 that confirms the account exists — the
    opposite of what a failed login is supposed to reveal.
    """
    assert verify_password("anything", "not-a-hash") is False
    assert verify_and_update_password("anything", "not-a-hash") == (False, None)


def test_the_dummy_verify_runs_without_raising() -> None:
    """Used on the unknown-email path to spend the same time a real check would.

    If this raised, the login endpoint would 500 for every address that has no
    account — which is both a worse experience and a louder enumeration signal than
    the timing difference it exists to hide.
    """
    dummy_verify("any-password-at-all")


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------


def test_an_access_token_round_trips() -> None:
    token = create_access_token(USER_ID, ORG_ID, UserRole.AGENT)
    claims = decode_access_token(token)

    assert claims.user_id == USER_ID
    assert claims.organization_id == ORG_ID
    assert claims.role is UserRole.AGENT


def test_the_subject_is_a_string_in_the_encoded_token() -> None:
    """PyJWT rejects a non-string `sub`, and the claim type is registered.

    Asserted explicitly because it is the kind of thing that works until a library
    upgrade enforces the spec, at which point every token in flight breaks.
    """
    token = create_access_token(USER_ID, ORG_ID, UserRole.ADMIN)
    raw = jwt.decode(token, get_settings().JWT_SECRET, algorithms=[get_settings().JWT_ALGORITHM])

    assert isinstance(raw["sub"], str)
    assert raw["sub"] == str(USER_ID)


def test_a_token_signed_with_another_secret_is_rejected() -> None:
    forged = jwt.encode(
        {
            "sub": str(USER_ID),
            "org": str(ORG_ID),
            "role": "admin",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        "an-entirely-different-secret-value-32ch",
        algorithm="HS256",
    )

    with pytest.raises(AuthenticationRequiredError):
        decode_access_token(forged)


def test_a_tampered_payload_is_rejected() -> None:
    token = create_access_token(USER_ID, ORG_ID, UserRole.AGENT)
    header, payload, signature = token.split(".")

    with pytest.raises(AuthenticationRequiredError):
        decode_access_token(f"{header}.{payload[:-3]}ABC.{signature}")


def test_an_expired_token_is_rejected() -> None:
    token = create_access_token(
        USER_ID, ORG_ID, UserRole.ADMIN, expires_delta=timedelta(seconds=-1)
    )

    with pytest.raises(AuthenticationRequiredError):
        decode_access_token(token)


def test_an_unsigned_token_is_rejected() -> None:
    """The `alg: none` forgery.

    Hand-assembled rather than produced by PyJWT, which will not emit an unsigned
    token — so this tests the decoder's algorithm pinning rather than the encoder's
    willingness. A decoder that honoured the token's own `alg` header would accept
    this, and anyone could then mint an admin token with a text editor.
    """

    def segment(value: dict[str, object]) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    forged = (
        f"{segment({'alg': 'none', 'typ': 'JWT'})}."
        f"{segment({'sub': str(USER_ID), 'org': str(ORG_ID), 'role': 'admin', 'exp': 9999999999})}."
    )

    with pytest.raises(AuthenticationRequiredError):
        decode_access_token(forged)


def test_a_token_missing_its_tenant_claim_is_rejected() -> None:
    """`org` is required, not optional.

    Without it a token would name a user but no tenant, and the dependency would have
    nothing to compare the user's row against.
    """
    token = jwt.encode(
        {
            "sub": str(USER_ID),
            "role": "admin",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        get_settings().JWT_SECRET,
        algorithm=get_settings().JWT_ALGORITHM,
    )

    with pytest.raises(AuthenticationRequiredError):
        decode_access_token(token)


def test_a_token_with_an_unparseable_role_is_rejected() -> None:
    token = jwt.encode(
        {
            "sub": str(USER_ID),
            "org": str(ORG_ID),
            "role": "superuser",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        get_settings().JWT_SECRET,
        algorithm=get_settings().JWT_ALGORITHM,
    )

    with pytest.raises(AuthenticationRequiredError):
        decode_access_token(token)


def test_garbage_is_rejected() -> None:
    with pytest.raises(AuthenticationRequiredError):
        decode_access_token("not-a-token")


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------


def test_refresh_tokens_are_unique_and_long() -> None:
    """256 bits from a CSPRNG — the only thing protecting a stored session."""
    tokens = {generate_refresh_token() for _ in range(100)}

    assert len(tokens) == 100
    # token_urlsafe(32) yields 43 characters.
    assert all(len(token) >= 43 for token in tokens)


def test_hashing_a_refresh_token_is_deterministic_and_hex() -> None:
    """Deterministic by necessity: the lookup is `WHERE token_hash = :hash`.

    A salted scheme would make that query impossible, which is why a plain SHA-256 is
    correct here and Argon2 is not — a 256-bit random string has no guessable
    structure for a slow hash to defend.
    """
    token = generate_refresh_token()
    digest = hash_refresh_token(token)

    assert digest == hash_refresh_token(token)
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


def test_distinct_refresh_tokens_hash_differently() -> None:
    assert hash_refresh_token(generate_refresh_token()) != hash_refresh_token(
        generate_refresh_token()
    )


def test_the_raw_token_is_not_recoverable_from_its_hash() -> None:
    token = generate_refresh_token()
    assert token not in hash_refresh_token(token)


def test_the_refresh_expiry_and_ttl_agree() -> None:
    """Both derive from one setting, so a cookie cannot outlive its token."""
    now = datetime.now(UTC)
    days = get_settings().REFRESH_TOKEN_EXPIRE_DAYS

    assert refresh_token_ttl_seconds() == days * 24 * 60 * 60
    assert (
        timedelta(days=days) - timedelta(seconds=5)
        < (refresh_token_expiry() - now)
        < timedelta(days=days) + timedelta(seconds=5)
    )
