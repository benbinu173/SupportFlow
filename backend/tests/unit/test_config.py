"""Configuration tests.

Regression coverage for two real failures found during Phase C setup:
comma-separated CORS origins, and secrets silently defaulting.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    # Object-storage credentials, required and defaultless for the same reason the
    # three above are: a missing secret must stop the process, not pick a fallback.
    "S3_ACCESS_KEY": "minioadmin",
    "S3_SECRET_KEY": "minioadmin",
}


def _settings(**overrides: str) -> Settings:
    # _env_file=None isolates the test from any real .env on disk.
    return Settings(**{**BASE_ENV, **overrides}, _env_file=None)  # type: ignore[arg-type]


@pytest.mark.unit
def test_cors_origins_splits_comma_separated_value() -> None:
    settings = _settings(CORS_ORIGINS="http://localhost:5173,https://app.example.com")

    assert settings.cors_origins == ["http://localhost:5173", "https://app.example.com"]


@pytest.mark.unit
def test_cors_origins_tolerates_whitespace_and_empty_entries() -> None:
    settings = _settings(CORS_ORIGINS=" http://a.test , ,http://b.test ")

    assert settings.cors_origins == ["http://a.test", "http://b.test"]


@pytest.mark.unit
def test_single_origin_yields_one_entry() -> None:
    assert _settings(CORS_ORIGINS="http://localhost:5173").cors_origins == ["http://localhost:5173"]


@pytest.mark.unit
def test_short_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        _settings(JWT_SECRET="too-short")


@pytest.mark.unit
@pytest.mark.parametrize(
    "secret_field", ["DATABASE_URL", "REDIS_URL", "JWT_SECRET", "S3_ACCESS_KEY", "S3_SECRET_KEY"]
)
def test_required_settings_have_no_default(
    secret_field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing secret must fail at startup, never fall back to a default.

    conftest populates these in os.environ so the app is importable, and
    `_env_file=None` only suppresses dotenv — not the process environment. The
    variable therefore has to be unset explicitly for this to test anything.
    """
    for key in BASE_ENV:
        monkeypatch.delenv(key, raising=False)
    env = {k: v for k, v in BASE_ENV.items() if k != secret_field}

    with pytest.raises(ValidationError, match=secret_field):
        Settings(**env, _env_file=None)  # type: ignore[arg-type]


@pytest.mark.unit
def test_is_production_flag() -> None:
    assert _settings(ENVIRONMENT="production").is_production is True
    assert _settings(ENVIRONMENT="development").is_production is False


@pytest.mark.unit
def test_access_token_ttl_is_short_by_default() -> None:
    """Access tokens are unrevokable, so a short TTL is the revocation mechanism."""
    assert _settings().ACCESS_TOKEN_EXPIRE_MINUTES <= 30
