"""Application configuration.

Settings are read from the environment (and a local .env during development).
Secrets never carry defaults — a missing secret must fail loudly at startup rather
than silently falling back to something insecure.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: config.py → core → app → backend → root. The .env lives there so a
# single file serves the API, the worker, and docker compose. Resolved absolutely
# because relative lookup would depend on the process working directory.
_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # backend/.env wins when present, for per-service overrides.
        env_file=(_ROOT_ENV, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ------------------------------------------------------
    ENVIRONMENT: Literal["development", "test", "production"] = "development"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    PROJECT_NAME: str = "SupportFlow"

    # --- Datastores -------------------------------------------------------
    DATABASE_URL: PostgresDsn
    REDIS_URL: RedisDsn

    # --- Celery -----------------------------------------------------------
    # Read here for the first time in Phase P. Compose and .env.example have carried
    # these since Phase C, but a setting with no consumer is a guess, so they waited for
    # the code that uses them — the same rule `S3_*` followed before Phase L.
    #
    # db 2, separate from the cache/limits db 0: a `FLUSHDB` run to clear the rate-limit
    # counters must not be able to discard queued work. ADR-022 records the split.
    CELERY_BROKER_URL: RedisDsn = RedisDsn("redis://localhost:6379/2")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn("redis://localhost:6379/2")

    # A hung SMTP conversation must not pin a worker slot forever. The hard limit is the
    # one that matters — Celery's soft limit raises inside the task and can be caught,
    # the hard one kills the process, which is what a worker stuck in a socket read
    # actually needs.
    CELERY_TASK_SOFT_TIME_LIMIT_SECONDS: int = 60
    CELERY_TASK_TIME_LIMIT_SECONDS: int = 120

    # --- Email ------------------------------------------------------------
    # Mailpit locally, any SMTP provider in production. Host, port, and sender carry
    # defaults for the same reason `S3_ENDPOINT` does: none is a secret and each has an
    # obvious local value. The credentials below do not, so they default to `None`.
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 1025
    SMTP_FROM: str = "support@supportflow.local"
    SMTP_TIMEOUT_SECONDS: int = 10

    # Optional, because Mailpit accepts mail from anyone and a real provider does not.
    # `None` means "do not authenticate", which is what a local mail catcher wants — an
    # empty string would be a credential that is present and wrong.
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None

    # Off by default, and explicitly configurable rather than inferred from the port.
    # Port 1025 is plaintext and port 587 speaks STARTTLS, but the port number is a
    # convention and this is a security decision: a deployment that leaves it off
    # against a provider expecting TLS should be a visible configuration, not a guess
    # the application made on its behalf.
    SMTP_STARTTLS: bool = False

    # --- Auth -------------------------------------------------------------
    # No defaults: see module docstring.
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Length only. Modern guidance (NIST SP 800-63B) is to prefer length over
    # character-class rules, which push users toward predictable substitutions
    # without meaningfully raising entropy.
    PASSWORD_MIN_LENGTH: int = 12

    # Refuse anything longer than the hash function can take in one pass. Argon2 has
    # no 72-byte truncation trap the way bcrypt does, so this is purely a
    # denial-of-service ceiling on hashing an absurd payload.
    PASSWORD_MAX_LENGTH: int = 128

    # The refresh cookie is the only credential the browser stores, so its scope is
    # kept as narrow as possible. See `refresh_cookie_path` for the path default.
    REFRESH_COOKIE_NAME: str = "sf_refresh"

    # --- Rate limiting ----------------------------------------------------
    # Per client IP. Login is generous enough for a typo-prone human and tight enough
    # to make credential stuffing expensive; registration is tighter because an
    # account-creation endpoint has no legitimate high-frequency use.
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10
    RATE_LIMIT_REGISTER_PER_HOUR: int = 5

    # Per *user*, not per address — the upload endpoint is authenticated, so identity
    # exists and is the precise thing to count (§45). 60 is one a minute sustained for
    # an hour: far above what a person attaching screenshots and logs to tickets would
    # reach, far below what a script filling the bucket needs. See
    # `app/api/rate_limits.py`, which is the whole limit surface in one file.
    RATE_LIMIT_UPLOAD_PER_HOUR: int = 60

    # --- Object storage ---------------------------------------------------
    # MinIO locally, any S3-compatible service in production. The endpoint and region
    # carry defaults because neither is a secret and both have an obvious local value;
    # the credentials do not, per the module docstring.
    S3_ENDPOINT: str = "http://localhost:9000"
    S3_ACCESS_KEY: str
    S3_SECRET_KEY: str
    S3_BUCKET: str = "supportflow-attachments"
    S3_REGION: str = "us-east-1"

    # The upload ceiling, enforced by counting bytes as they stream rather than by
    # trusting the request's Content-Length — see `app/services/attachment_service.py`.
    # 25 MiB is comfortably above any screenshot or log a support desk would attach and
    # well below the point where proxying the bytes through the API stops being
    # reasonable.
    MAX_ATTACHMENT_BYTES: int = 25 * 1024 * 1024

    # --- CORS -------------------------------------------------------------
    # Explicit allowlist. Required because the refresh cookie is sent with
    # credentials, which forbids a wildcard origin.
    #
    # Held as a raw string rather than list[str]: pydantic-settings JSON-decodes
    # complex types straight from dotenv, before any validator runs, so a
    # comma-separated value would raise instead of being split. Parsing happens
    # in the `cors_origins` property below.
    CORS_ORIGINS: str = "http://localhost:5173"

    @property
    def cors_origins(self) -> list[str]:
        """CORS_ORIGINS split into a list, empty entries dropped."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def sqlalchemy_dsn(self) -> str:
        """DATABASE_URL with the driver made explicit.

        Written as `postgresql://` in .env because that is what psql, pg_dump, and
        docker compose all understand. SQLAlchemy would map that bare scheme to the
        default DBAPI — psycopg2, which is not installed — so psycopg 3 is named
        here instead. Shared by the app engine and by Alembic so the two cannot
        disagree about which driver they are using.
        """
        dsn = str(self.DATABASE_URL)
        if dsn.startswith("postgresql+"):
            return dsn
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)

    @field_validator("JWT_SECRET")
    @classmethod
    def _secret_is_strong_enough(cls, v: str) -> str:
        # PyJWT warns below 32 bytes for HS256; treat it as an error instead.
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @field_validator("SMTP_USERNAME", "SMTP_PASSWORD", mode="before")
    @classmethod
    def _blank_credential_is_absent(cls, v: object) -> object:
        """Treat an empty string as "not set".

        `.env.example` ships both keys with no value, so a developer who copies it has
        `SMTP_USERNAME=""` in the environment. Without this, the mail layer would try to
        authenticate with an empty username — a credential that is present and wrong,
        which fails differently from one that is absent and is harder to read.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def refresh_cookie_path(self) -> str:
        """Scope the refresh cookie to the auth routes.

        Narrower than `/`, so the browser does not attach a long-lived credential to
        every request the API serves — only the endpoints that can actually use it.
        """
        return f"{self.API_V1_PREFIX}/auth"

    @property
    def refresh_cookie_secure(self) -> bool:
        """`Secure` outside development.

        Development is served over plain HTTP on localhost, where a `Secure` cookie
        is silently dropped by the browser — the failure would look like "refresh
        randomly does not work" rather than a configuration error. Enabled the moment
        the environment is not development.
        """
        return self.ENVIRONMENT != "development"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so configuration is parsed and validated exactly once."""
    return Settings()
