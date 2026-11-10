"""Verification credentials cannot authorize sessions or reset passwords."""

import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from app.core.config import get_settings
from app.core.exceptions import (
    AuthenticationRequiredError,
    InvalidResetTokenError,
    InvalidVerificationTokenError,
)
from app.core.security import (
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
    decode_access_token,
    decode_email_verification_token,
    decode_password_reset_token,
    email_verification_fingerprint,
)
from app.models.enums import UserRole

pytestmark = pytest.mark.unit
USER_ID, ORG_ID = uuid.uuid4(), uuid.uuid4()
EMAIL = "owner@example.com"
AUDIENCE = "supportflow:email-verification"


def token(*, expired: bool = False) -> str:
    return create_email_verification_token(
        USER_ID,
        ORG_ID,
        email_verification_fingerprint(EMAIL),
        expires_at=datetime.now(UTC) + timedelta(minutes=-1 if expired else 60),
    )


def test_verification_is_separate_from_both_other_credentials() -> None:
    verification = token()
    with pytest.raises(AuthenticationRequiredError):
        decode_access_token(verification)
    with pytest.raises(InvalidResetTokenError):
        decode_password_reset_token(verification)
    for other in (
        create_access_token(USER_ID, ORG_ID, UserRole.ADMIN),
        create_password_reset_token(
            USER_ID, ORG_ID, 0, expires_at=datetime.now(UTC) + timedelta(minutes=30)
        ),
    ):
        with pytest.raises(InvalidVerificationTokenError):
            decode_email_verification_token(other)


def test_verified_claims_name_one_account_and_one_address() -> None:
    claims = decode_email_verification_token(token())
    assert (claims.user_id, claims.organization_id) == (USER_ID, ORG_ID)
    assert claims.email_hash == email_verification_fingerprint(EMAIL)
    assert claims.email_hash != email_verification_fingerprint("other@example.com")
    assert EMAIL not in jwt.decode(token(), options={"verify_signature": False}).values()


def test_an_expired_or_tampered_link_is_refused() -> None:
    for value in (token(expired=True), token() + "tampered", "not-a-token"):
        with pytest.raises(InvalidVerificationTokenError):
            decode_email_verification_token(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sub", None),
        ("sub", 123),
        ("sub", "not-a-uuid"),
        ("org", []),
        ("org", "not-a-uuid"),
        ("email_hash", True),
        ("email_hash", "g" * 64),
        ("email_hash", "a" * 63),
        ("aud", "supportflow:password-reset"),
        ("exp", []),
    ],
)
def test_signed_but_malformed_claims_are_a_public_link_error(field: str, value: Any) -> None:
    payload = jwt.decode(token(), options={"verify_signature": False})
    payload[field] = value
    key = hmac.digest(get_settings().JWT_SECRET.encode(), AUDIENCE.encode(), "sha256")
    malformed = jwt.encode(payload, key, algorithm=get_settings().JWT_ALGORITHM)
    with pytest.raises(InvalidVerificationTokenError):
        decode_email_verification_token(malformed)


@pytest.mark.parametrize("field", ["sub", "org", "email_hash", "aud", "exp"])
def test_required_claims_cannot_be_omitted(field: str) -> None:
    payload = jwt.decode(token(), options={"verify_signature": False})
    del payload[field]
    key = hmac.digest(get_settings().JWT_SECRET.encode(), AUDIENCE.encode(), "sha256")
    malformed = jwt.encode(payload, key, algorithm=get_settings().JWT_ALGORITHM)
    with pytest.raises(InvalidVerificationTokenError):
        decode_email_verification_token(malformed)
