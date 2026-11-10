"""Recovery credentials are short-lived, purpose-bound, and versioned."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationRequiredError, InvalidResetTokenError
from app.core.security import (
    create_access_token,
    create_password_reset_token,
    decode_access_token,
    decode_password_reset_token,
)
from app.models.enums import UserRole

pytestmark = pytest.mark.unit


def test_recovery_link_cannot_authenticate_as_an_access_token() -> None:
    token = create_password_reset_token(
        uuid.uuid4(), uuid.uuid4(), 4, expires_at=datetime.now(UTC) + timedelta(minutes=30)
    )
    assert decode_password_reset_token(token).token_version == 4
    with pytest.raises(AuthenticationRequiredError):
        decode_access_token(token)


def test_access_token_cannot_reset_a_password() -> None:
    token = create_access_token(uuid.uuid4(), uuid.uuid4(), UserRole.AGENT)
    with pytest.raises(InvalidResetTokenError):
        decode_password_reset_token(token)


def test_expired_link_is_invalid() -> None:
    token = create_password_reset_token(
        uuid.uuid4(), uuid.uuid4(), 0, expires_at=datetime.now(UTC) - timedelta(seconds=2)
    )
    with pytest.raises(InvalidResetTokenError):
        decode_password_reset_token(token)


@pytest.mark.parametrize("token", ["not-a-token", "", "a.b.c"])
def test_malformed_links_share_the_recovery_error(token: str) -> None:
    with pytest.raises(InvalidResetTokenError):
        decode_password_reset_token(token)


def test_legacy_access_tokens_default_to_the_initial_version() -> None:
    settings = get_settings()
    token = create_access_token(uuid.uuid4(), uuid.uuid4(), UserRole.AGENT)
    claims = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    claims.pop("ver")
    legacy = jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    assert decode_access_token(legacy).token_version == 0


@pytest.mark.parametrize("version", [True, -1, "1", None])
def test_signed_invalid_access_versions_are_rejected(version: object) -> None:
    settings = get_settings()
    token = create_access_token(uuid.uuid4(), uuid.uuid4(), UserRole.AGENT)
    claims = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    claims["ver"] = version
    token = jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    with pytest.raises(AuthenticationRequiredError):
        decode_access_token(token)


@pytest.mark.parametrize(
    "url",
    [
        "http://support.example.com",
        "https://user:password@support.example.com",
        "https://support.example.com/path",
        "https://support.example.com?next=elsewhere",
        "https://support.example.com#token=anything",
    ],
)
def test_email_origin_cannot_redirect_or_expose_a_reset_credential(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(PUBLIC_APP_URL=url)


@pytest.mark.parametrize("url", ["http://localhost:5173", "https://support.example.com"])
def test_local_and_https_origins_are_supported(url: str) -> None:
    assert str(Settings(PUBLIC_APP_URL=url).PUBLIC_APP_URL).rstrip("/") == url
