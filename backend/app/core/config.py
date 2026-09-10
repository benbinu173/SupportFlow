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

    # --- Auth -------------------------------------------------------------
    # No defaults: see module docstring.
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

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

    @field_validator("JWT_SECRET")
    @classmethod
    def _secret_is_strong_enough(cls, v: str) -> str:
        # PyJWT warns below 32 bytes for HS256; treat it as an error instead.
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so configuration is parsed and validated exactly once."""
    return Settings()
