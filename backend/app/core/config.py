"""Application configuration.

Settings are read from the environment (and a local .env during development).
Secrets never carry defaults — a missing secret must fail loudly at startup rather
than silently falling back to something insecure.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, ValidationInfo, field_validator, model_validator
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

    # --- SLA ---------------------------------------------------------------
    # How often beat runs the deadline sweep, and how many tickets one priority's pass
    # looks at. Both arrived with their consumer in Phase Q, per the rule above.
    #
    # The interval is the knob that decides how late an alert can be, and the shortest
    # §27 target is the one that sets the requirement: URGENT's response target is 30
    # minutes with the warning at 80%, so a warning is due 24 minutes after creation and
    # a 300-second sweep bounds how late it fires. At the other end — LOW, 24 hours to
    # response, 19.2 hours to the warning — the same sweep is noise.
    SLA_SWEEP_INTERVAL_SECONDS: int = 300

    # A bound rather than an expectation: one organization's backlog must not make a
    # single task run unbounded. 500 covers any realistic tenant, and the timeline guard
    # means the work per ticket shrinks as the sweep catches up — a warning fires once,
    # so a ticket already alerted costs a `NOT EXISTS` and nothing more.
    SLA_SWEEP_BATCH_SIZE: int = 500

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

    # --- WebSockets --------------------------------------------------------
    # Three settings, each with the consumer it arrived with, per the rule the Celery and
    # S3 blocks above follow.
    #
    # **The auth window is the one that matters.** A socket is accepted before it knows who
    # is calling — a browser cannot set an `Authorization` header on a handshake — so an
    # unauthenticated connection exists for as long as this timeout allows. 10 seconds is
    # generous for a frame that contains one string and is already in memory, and short
    # enough that a port scanner opening ten thousand sockets and saying nothing goes
    # nowhere. See ADR-025.
    WS_AUTH_TIMEOUT_SECONDS: int = 10

    # How many events one connection may have queued before it is disconnected. The queue is
    # what stops a client that has stopped reading from delaying every other tenant's
    # delivery, and the bound is what stops it from holding an unbounded backlog of events the
    # client will never render — it re-reads state over HTTP when it reconnects, so a deeper
    # queue would only delay that. 64 is several screens of changes for a client that is
    # briefly busy and nowhere near enough to hold a backlog for one that is gone.
    WS_QUEUE_MAX_DEPTH: int = 64

    # A single send's ceiling. Bounds the case the queue cannot see: a half-open connection
    # that accepts bytes into a buffer nothing will ever drain, so no exception is raised and
    # the queue never fills. The writer task gives up on the connection rather than holding
    # the slot indefinitely.
    WS_SEND_TIMEOUT_SECONDS: float = 10.0

    # --- Analytics ---------------------------------------------------------
    # One setting, and it arrived with its consumer per the rule the Celery, S3, and
    # WebSockets blocks above follow: §15 asks for cached dashboard results and this is how
    # long one lives.
    #
    # 300 seconds is chosen against what makes an analytics number *wrong* rather than
    # against how expensive the queries are. Five minutes is a refresh nobody notices while
    # flipping between screens, and it is short enough that a dashboard left open all day is
    # never more than five minutes behind — which matters only in the one case the version
    # counter cannot cover: a Redis outage during a write, where the version does not move
    # and the old entry survives until this expires (ADR-026).
    #
    # The window default and the window ceiling are **not** settings. They are route
    # literals, like `limit`'s bounds — no deployment has a reason to change them, and a
    # setting for something nothing sets is a guess with a name.
    ANALYTICS_CACHE_TTL_SECONDS: int = 300

    # --- AI ----------------------------------------------------------------
    # The `--- AI ---` block has been in `.env.example` since Phase C; these are the
    # settings it was waiting to describe, arriving with the consumer per the rule the
    # Celery, S3, WebSockets, and Analytics blocks above follow.
    #
    # Which implementation `app/ai/provider.py`'s protocol is bound to. A `Literal` rather
    # than a free string so a typo is an error at startup rather than a provider that
    # cannot be found at the first ticket.
    #
    # Two real vendors, and that is the point of the interface: Anthropic serves the
    # `claude-*` models and Groq the `openai/*` one, and the pairing is checked below.
    AI_PROVIDER: Literal["anthropic", "groq", "fake"] = "anthropic"

    # Optional, following the `SMTP_USERNAME`/`SMTP_PASSWORD` precedent rather than the
    # S3/JWT required-and-defaultless one. The difference is that AI is a feature a
    # deployment can simply not have: a checkout with no key should run the whole test
    # suite, and a self-hosted installation that does not want AI should start. So the
    # key is absent-and-legal here, and the provider refuses to build a client without
    # one — the failure is loud at the point of use rather than at import.
    #
    # **One key, not one per vendor.** It is the credential *for `AI_PROVIDER`*, and its
    # shape follows from that: `sk-ant-…` for Anthropic, `gsk_…` for Groq. A second variable
    # would be a setting one of the two providers never reads, which is the rule this file
    # applies to every other unused key — and a deployment configures one provider at a time,
    # so there is nothing a second variable would let it say.
    #
    # §4 forbids secrets in source and this is why it is read from the environment and
    # never logged; see `app/ai/claude.py` on why an SDK error's own message is not
    # passed along, either.
    AI_API_KEY: str | None = None

    # Validated against `app/ai/pricing.py`'s rate table below. A model whose published
    # price is not known cannot be configured at all, because the alternative is a ledger
    # full of `cost_usd = 0` rows that a dashboard renders as "free" — a wrong number
    # rather than a missing one, which is the worse failure to debug. The table also says
    # which vendor serves each model, and that pairing is checked too — see below.
    AI_MODEL: str = "claude-sonnet-5"

    # One answer's ceiling. §20 summaries and §21 drafts are a few hundred tokens; 1024 is
    # several times the longest of them and still bounds the cost of a model that decides
    # to be expansive. Reaching it is a handled failure — `claude.py` reports the
    # truncation as an `AIOutputError` rather than half-parsing the arguments — and the
    # fix is to raise this number, which is why the error says so.
    AI_MAX_TOKENS: int = 1024

    # A support agent is waiting for a draft and a worker is holding a slot. 30 seconds is
    # well past a p95 answer for prompts of this size and short enough that a provider
    # that has stopped responding is abandoned while somebody still remembers asking.
    AI_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)

    # **Three attempts, not five.** `app/workers/email_tasks.py` retries five times over
    # minutes because an email is delivered eventually and a mail server that is down is
    # down for a while. An AI call is different in both directions: the caller is a person
    # watching a spinner, so the whole sequence has to fit inside a request; and the
    # failures that actually happen are a burst rate limit or a dropped connection, which
    # clear in seconds or not at all. Three attempts spanning roughly three seconds turns a
    # blip into a success and gives up on a real outage instead of hammering it.
    #
    # Bounded below at one because zero attempts is not a policy — it is a call path that
    # never calls anything, which fails in a way that reads like a bug in the loop.
    AI_MAX_ATTEMPTS: int = Field(default=3, ge=1)

    # The base of the exponential backoff between attempts. Small because the budget is
    # small: attempts are at 0s, ~1s, ~3s, so the sequence fits in a request while still
    # giving a rate limiter room to forget about us. Jittered in `ai_service` so a hundred
    # workers throttled by the same limit do not return in lockstep.
    AI_RETRY_BACKOFF_SECONDS: float = Field(default=1.0, ge=0)

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

    @field_validator("SMTP_USERNAME", "SMTP_PASSWORD", "AI_API_KEY", mode="before")
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

    @field_validator("AI_PROVIDER")
    @classmethod
    def _fake_provider_is_test_only(cls, v: str, info: ValidationInfo) -> str:
        """Refuse the scripted provider outside the test environment.

        §60 forbids *"fake AI results"* in the final implementation, and ADR-008 says the
        fake *"exists for tests only"*. Both are statements about a deployment, and the
        only place that knows what kind of deployment this is, is here. A comment on the
        fake class would be a rule a future commit could not break; this is a rule that
        commit fails on.

        `ENVIRONMENT` is read from `info.data` rather than from a second settings object,
        which is what makes the failure happen during validation of the very configuration
        that asked for it.
        """
        if v == "fake" and info.data.get("ENVIRONMENT") != "test":
            raise ValueError(
                "AI_PROVIDER=fake is only allowed when ENVIRONMENT=test "
                "(spec §60: no fake AI results in the final implementation)"
            )
        return v

    @field_validator("AI_MODEL")
    @classmethod
    def _model_has_a_published_rate(cls, v: str) -> str:
        """Refuse a model `app/ai/pricing.py` cannot price.

        Imported inside the validator rather than at module scope because `app/ai/pricing.py`
        raises `AIPermanentError` through `app/ai/errors.py`, and configuration should not
        be the thing that makes the AI package importable at startup. The import is cheap
        and this runs once.
        """
        from app.ai.errors import AIPermanentError
        from app.ai.pricing import priced_models, rate_for

        try:
            rate_for(v)
        except AIPermanentError as exc:
            raise ValueError(
                f"AI_MODEL={v!r} has no published rate. Priced models: {', '.join(priced_models())}"
            ) from exc
        return v

    @model_validator(mode="after")
    def _the_model_is_served_by_the_provider(self) -> "Settings":
        """Refuse a model paired with a vendor that does not serve it.

        **This is the check that turns a 401 into a startup error.** With one vendor, "is this
        model priced?" was enough. With two, `AI_PROVIDER=groq` beside `AI_MODEL=claude-sonnet-5`
        passes that check — the rate exists — and then fails at the first real call, as a
        provider rejecting the key. The key was never the problem, and the message would have
        sent its reader to check a credential that is perfectly good. A wrong model is a
        configuration mistake, so it is reported by configuration, where the process refuses
        to start at all.

        Runs after the field validators, so `AI_MODEL` has already been proven priceable and
        `rate_for` cannot raise here for the reason it exists.

        The fake is exempt: it is reached only under `ENVIRONMENT=test`, it needs no vendor,
        and a test asserting retry behaviour has no interest in which real model is configured.
        """
        from app.ai.pricing import models_for, rate_for

        if self.AI_PROVIDER == "fake":
            return self

        served_by = rate_for(self.AI_MODEL).provider
        if served_by != self.AI_PROVIDER:
            raise ValueError(
                f"AI_MODEL={self.AI_MODEL!r} is served by {served_by!r}, not by "
                f"AI_PROVIDER={self.AI_PROVIDER!r}. Models for {self.AI_PROVIDER!r}: "
                f"{', '.join(models_for(self.AI_PROVIDER)) or '<none>'}"
            )
        return self

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
