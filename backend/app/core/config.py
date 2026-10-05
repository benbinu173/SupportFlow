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

    # The floor for what reaches the log stream. A `Literal` rather than a free string so a
    # typo is a startup error naming the variable, instead of a `logging` call that silently
    # falls back to WARNING and hides every info line in the deployment.
    #
    # Read by `app/core/logging.py`, which is the single place the level is applied — to
    # structlog's bound logger and to the root stdlib logger both, so a third-party library's
    # line and this project's line are filtered by the same number.
    LOG_LEVEL: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"

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

    # Also per user, and the tightest of the three despite being the same window shape.
    # The argument is §53 rather than §45: an upload costs object storage and an AI call
    # costs money per token, so the number that a runaway client can spend in an hour is
    # the thing being bounded. 30 is above what a person reviewing tickets reaches by
    # hand — the route is a button, and a ticket already analyzes itself on creation —
    # and far below what a loop over a ticket id would reach.
    RATE_LIMIT_AI_PER_HOUR: int = 30

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

    # --- Embeddings --------------------------------------------------------
    # Three settings mirroring the `AI_*` trio above rather than reusing it, and the reason
    # is that block's own: `AI_API_KEY` is *"the credential for `AI_PROVIDER`"*, and
    # embeddings come from a different vendor deliberately (ADR-008). One key covering two
    # vendors would be a setting that one of them never reads.
    #
    # Phase X bound this to OpenAI, the only vendor in `app/ai/pricing.py` that publishes an
    # embedding model — neither Anthropic nor Groq does, which is why ADR-027 kept
    # `generate_embedding` off `AIProvider` until there was an implementation behind it.
    EMBEDDING_PROVIDER: Literal["openai", "fake"] = "openai"

    # Optional, for the reason `AI_API_KEY` is optional: AI is a feature a deployment can
    # simply not have, so a checkout with no embedding key runs the whole test suite and
    # starts. The knowledge routes refuse at the point of use, loudly, rather than at import.
    # An empty string is absent — see `_blank_credential_is_absent`.
    EMBEDDING_API_KEY: str | None = None

    # Validated against the same rate table, by the same two checks: the model must be
    # priced, and it must be served by `EMBEDDING_PROVIDER`. `text-embedding-3-small` is 1536
    # dimensions, which is what `EMBEDDING_DIMENSIONS` declares on the column — one decision
    # written in two places, and changing either is a migration and a re-embed rather than a
    # config edit, as `app/models/knowledge_chunk.py` says.
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    # --- Retrieval ---------------------------------------------------------
    # **These two are the policy of when this product is allowed to answer**, which is why
    # they are settings and not constants in the service that reads them.
    #
    # §24's relevance threshold. Below it, the answer is that the knowledge base does not
    # contain sufficient information, and **no model is called at all** — so raising this
    # makes the product refuse more and invent less, and lowering it makes it answer more and
    # cite worse. 0.3 is a starting point for `text-embedding-3-small`, where a genuinely
    # relevant passage typically scores well above it and an unrelated one below; a deployment
    # with a corpus of its own is expected to move it after reading what its own questions
    # score. Bounded to a probability because cosine similarity is one.
    RETRIEVAL_MIN_SIMILARITY: float = Field(default=0.3, ge=0, le=1)

    # How many passages reach the prompt. Bounded above at 50 because this is a ceiling on
    # what one question may cost — every passage is tokens sent to a model — and a deployment
    # that asked for a thousand would be buying a context window rather than an answer. Five
    # is §23's "top relevant chunks": several chances to catch the right passage, few enough
    # that the prompt stays about the question.
    RETRIEVAL_TOP_K: int = Field(default=5, ge=1, le=50)

    # --- Knowledge base ----------------------------------------------------
    # The ingestion ceiling, in the shape `MAX_ATTACHMENT_BYTES` established and enforced the
    # same way: by counting bytes as they stream, never by trusting `Content-Length`. It is
    # smaller than the attachment ceiling on purpose, and the difference is what the file
    # becomes: an attachment is stored and served back, while a document is *read*, split, and
    # embedded, and every byte of it is tokens this deployment pays for. Ten mebibytes of
    # prose is roughly two and a half million tokens, which is already far past any policy
    # document a support desk has.
    MAX_KNOWLEDGE_DOCUMENT_BYTES: int = 10 * 1024 * 1024

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

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _the_log_level_is_case_insensitive(cls, v: object) -> object:
        """Accept `info` as `INFO`, because this file's convention is lowercase.

        Every other enum-like value in the repository's `.env` is written lowercase —
        `ENVIRONMENT=development`, `AI_PROVIDER=anthropic`, `EMBEDDING_PROVIDER=openai` — so
        `LOG_LEVEL=info` is the spelling this project teaches, and refusing it in favour of an
        uppercase form stdlib happens to use would be a trap laid by a library's convention
        rather than this project's.

        The value is normalised to uppercase rather than the `Literal` being widened, because
        stdlib's `logging` is the consumer and its table is uppercase: `setLevel("info")` raises.
        Normalising here means the field holds the one spelling every downstream use expects.
        """
        if isinstance(v, str):
            return v.upper()
        return v

    @field_validator("JWT_SECRET")
    @classmethod
    def _secret_is_strong_enough(cls, v: str) -> str:
        # PyJWT warns below 32 bytes for HS256; treat it as an error instead.
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @field_validator(
        "SMTP_USERNAME", "SMTP_PASSWORD", "AI_API_KEY", "EMBEDDING_API_KEY", mode="before"
    )
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

    @field_validator("AI_MODEL", "EMBEDDING_MODEL", mode="before")
    @classmethod
    def _blank_model_is_the_default(cls, v: object, info: ValidationInfo) -> object:
        """Treat an empty model name as "not set", so the field's default applies.

        **`_blank_credential_is_absent`'s argument, one field type over.** `.env.example` ships
        the model keys with no value next to the credentials, and a developer who copies it gets
        `EMBEDDING_MODEL=""` in the environment — which pydantic-settings reads as a *value*, not
        as an absence, so the default would never be reached. The startup then fails on
        `_model_has_a_published_rate` with *"'' has no published rate"*, a message about a model
        nobody chose.

        The blank is a real thing to write — it is how the example file says "you do not have to
        set this" — so it is understood rather than forbidden. A typo is unaffected: only the
        empty string is redirected, and any other unpriceable name still fails loudly.

        Read from `model_fields` rather than restated, so the default has one home and this
        cannot drift from the field it serves. `info.field_name` is optional in pydantic's
        types — it is always set here, because the decorator names both fields — and the lookup
        is written to tolerate its absence rather than to assert it away.
        """
        field = cls.model_fields.get(info.field_name or "")
        if isinstance(v, str) and not v.strip() and field is not None:
            return field.default
        return v

    @field_validator("AI_PROVIDER", "EMBEDDING_PROVIDER")
    @classmethod
    def _fake_provider_is_test_only(cls, v: str, info: ValidationInfo) -> str:
        """Refuse a scripted provider outside the test environment.

        §60 forbids *"fake AI results"* in the final implementation, and ADR-008 says the fake
        *"exists for tests only"*. Both are statements about a deployment, and the only place
        that knows what kind of deployment this is, is here. A comment on the fake class would
        be a rule a future commit could not break; this is a rule that commit fails on.

        **One validator for both selectors**, because the rule is about the word `fake` rather
        than about which of the two protocols it is bound to — and two copies of it would be
        two places to forget when a third provider arrives. The message names the field that
        failed, so a deployment running from a `.env.example` it copied learns which line it
        left set.

        `ENVIRONMENT` is read from `info.data` rather than from a second settings object,
        which is what makes the failure happen during validation of the very configuration
        that asked for it.
        """
        if v == "fake" and info.data.get("ENVIRONMENT") != "test":
            raise ValueError(
                f"{info.field_name}=fake is only allowed when ENVIRONMENT=test "
                "(spec §60: no fake AI results in the final implementation)"
            )
        return v

    @field_validator("AI_MODEL", "EMBEDDING_MODEL")
    @classmethod
    def _model_has_a_published_rate(cls, v: str, info: ValidationInfo) -> str:
        """Refuse a model `app/ai/pricing.py` cannot price.

        One validator for both models, because the question is the same one: is this name in
        the rate table at all? Which vendor serves it is the *next* check, and it is the one
        that can say what the right name would be.

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
                f"{info.field_name}={v!r} has no published rate. "
                f"Priced models: {', '.join(priced_models())}"
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

        Runs after the field validators, so both models have already been proven priceable and
        `rate_for` cannot raise here for the reason it exists.

        **Phase X gave it a second pairing rather than a second validator.** Generation and
        embedding are two vendors by design (ADR-008), so `AI_MODEL`/`AI_PROVIDER` and
        `EMBEDDING_MODEL`/`EMBEDDING_PROVIDER` are two independent pairings — and this is the
        one place that answers "which vendor serves this name?", which is the question both
        are asking.

        Each is skipped when its provider is the fake: that one is reached only under
        `ENVIRONMENT=test`, needs no vendor, and a test asserting retry behaviour has no
        interest in which real model is configured.
        """
        from app.ai.pricing import models_for, rate_for

        if self.AI_PROVIDER != "fake":
            served_by = rate_for(self.AI_MODEL).provider
            if served_by != self.AI_PROVIDER:
                raise ValueError(
                    f"AI_MODEL={self.AI_MODEL!r} is served by {served_by!r}, not by "
                    f"AI_PROVIDER={self.AI_PROVIDER!r}. Models for {self.AI_PROVIDER!r}: "
                    f"{', '.join(models_for(self.AI_PROVIDER)) or '<none>'}"
                )

        if self.EMBEDDING_PROVIDER != "fake":
            embedding_vendor = rate_for(self.EMBEDDING_MODEL).provider
            if embedding_vendor != self.EMBEDDING_PROVIDER:
                raise ValueError(
                    f"EMBEDDING_MODEL={self.EMBEDDING_MODEL!r} is served by "
                    f"{embedding_vendor!r}, not by "
                    f"EMBEDDING_PROVIDER={self.EMBEDDING_PROVIDER!r}. Models for "
                    f"{self.EMBEDDING_PROVIDER!r}: "
                    f"{', '.join(models_for(self.EMBEDDING_PROVIDER)) or '<none>'}"
                )
        return self

    @model_validator(mode="after")
    def _production_is_not_a_development_checkout(self) -> "Settings":
        """Refuse a production deployment still wearing development settings.

        **Added in Phase Y, and the two rules are the two ways this actually happens.** Both
        describe a deployment that starts cleanly and is wrong in a way nothing else reports:
        no test fails, no route changes shape, and the problem is discovered by a stranger.

        1. **`DEBUG=true` beside `ENVIRONMENT=production`.** `DEBUG` is not a log level here —
         it is the flag a developer sets to get a verbose local run, and it is exactly the flag
         that gets left on when a `.env` is copied to a server. Production posture and debug
         posture are contradictory claims about the same process, so the process refuses to
         make both.

        2. **A `JWT_SECRET` that this repository publishes.** Every value below is committed in
         plaintext — `.env.example`'s template or `ci.yml`'s CI-only secret. A signing key
         anyone can read is not a signing key; it is a key that lets anyone mint a token for any
         tenant, which is the one credential in this system whose compromise is total. The check
         is a blocklist rather than an entropy heuristic because a blocklist of *the values this
         repo itself ships* is finite, knowable, and exactly the failure being prevented —
         guessing at what a weak secret looks like is not.

        Deliberately a `model_validator` rather than two `field_validator`s: `DEBUG` alone is
        legal, `ENVIRONMENT=production` alone is legal, and only the pair is a contradiction.
        Compare `_the_model_is_served_by_the_provider`, which is a pairing for the same reason.
        """
        if not self.is_production:
            return self

        if self.DEBUG:
            raise ValueError(
                "DEBUG=true is refused when ENVIRONMENT=production. Set DEBUG=false, or "
                "set ENVIRONMENT=development if this really is a development machine."
            )

        published = {
            # .env.example's template value, and the shape a person types by hand.
            "change-me-in-production-at-least-32-chars",
            "change-me",
            # ci.yml's value. Public in this repository by construction.
            "ci-only-secret-value-at-least-32-chars",
        }
        if self.JWT_SECRET in published:
            raise ValueError(
                "JWT_SECRET is a value published in this repository (.env.example or "
                "ci.yml), so it is not a secret. Generate one: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
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
