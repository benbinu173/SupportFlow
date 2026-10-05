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


@pytest.mark.unit
def test_the_upload_limit_is_on_by_default_and_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is what stands between a fresh deployment and no upload limit.

    Every guard in `app/api/rate_limits.py` reads this setting per request, so a
    default that quietly became 0 — or a field that stopped existing — would disable an
    abuse control without failing anything. The explicit 60 is deliberate: changing a
    security default should mean editing a test that names it, and `.env.example` and
    `config.py` carry the reasoning for why it is 60 rather than 5 or 500.

    conftest sets this variable in `os.environ` so the suite stays off the limiter, and
    `_env_file=None` suppresses dotenv but not the process environment — so, exactly as
    in `test_required_settings_have_no_default`, it has to be unset explicitly before
    the default is observable at all.
    """
    monkeypatch.delenv("RATE_LIMIT_UPLOAD_PER_HOUR", raising=False)

    assert _settings().RATE_LIMIT_UPLOAD_PER_HOUR == 60
    assert _settings(RATE_LIMIT_UPLOAD_PER_HOUR="2").RATE_LIMIT_UPLOAD_PER_HOUR == 2


# ---------------------------------------------------------------------------
# AI (§17 configuration, and §60's guard on the fake provider)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_ai_provider_defaults_to_the_real_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A default is what a deployment gets when nobody decides.

    `conftest` pins this variable so the suite's cost figures do not move with a developer's
    `.env`, and `_env_file=None` suppresses dotenv without touching the process environment —
    so the default is only observable once the variable is unset explicitly. Same pattern and
    same reason as `test_the_upload_limit_is_on_by_default_and_tunable` above.
    """
    monkeypatch.delenv("AI_PROVIDER", raising=False)

    assert _settings().AI_PROVIDER == "anthropic"


@pytest.mark.unit
@pytest.mark.parametrize("environment", ["development", "production"])
def test_the_fake_provider_is_refused_outside_tests(environment: str) -> None:
    """§60: the final implementation must not *"use fake AI results"*.

    ADR-008 says the fake *"exists for tests only"*, and a comment saying so is a rule the
    next commit cannot break. This is the rule that commit fails on — the guard is in
    configuration, so there is no code path that constructs a fake provider in a
    deployment that asked for one.

    Both non-test environments are checked, because the failure mode of an `!= "test"`
    condition written as `== "development"` is that production silently gets a scripted
    provider that answers nothing.
    """
    with pytest.raises(ValidationError, match="AI_PROVIDER=fake"):
        _settings(AI_PROVIDER="fake", ENVIRONMENT=environment)


@pytest.mark.unit
def test_the_fake_provider_is_allowed_in_tests() -> None:
    """The same setting, one environment over — so the guard is narrow and not a ban."""
    assert _settings(AI_PROVIDER="fake", ENVIRONMENT="test").AI_PROVIDER == "fake"


@pytest.mark.unit
def test_an_unpriced_ai_model_is_refused() -> None:
    """The failure `app/ai/pricing.py` exists to prevent, caught at startup instead.

    A model whose published rate is unknown would write `cost_usd = 0` for every call it
    served, and a dashboard renders that as "free" rather than as "unpriced" — a wrong
    number rather than a missing one.
    """
    with pytest.raises(ValidationError, match="has no published rate"):
        _settings(AI_MODEL="gpt-4-turbo")


@pytest.mark.unit
def test_a_priced_ai_model_is_accepted() -> None:
    assert _settings(AI_MODEL="claude-haiku-4-5-20251001").AI_MODEL == ("claude-haiku-4-5-20251001")


@pytest.mark.unit
def test_a_model_served_by_another_provider_is_refused() -> None:
    """The check that turns a 401 into a startup error (ADR-028).

    `claude-sonnet-5` *is* a priced model, so it passes the rate check above and then reaches
    a Groq endpoint holding a Groq key — a request that fails with the provider rejecting a
    credential which is in fact perfectly good. The reader of that error would go and check
    their key, and the key is not the problem. So the pairing is refused here, where the
    message can name both halves and list what would work instead.

    This is not hypothetical: it is exactly the configuration that was in `.env` when the
    Groq key was added, and it is why the failure was caught by reading rather than by a
    500 from the first ticket.
    """
    with pytest.raises(
        ValidationError, match="is served by 'anthropic', not by AI_PROVIDER='groq'"
    ):
        _settings(AI_PROVIDER="groq", AI_MODEL="claude-sonnet-5")


@pytest.mark.unit
def test_the_pairing_error_says_which_models_would_work() -> None:
    """A validator that only reports the mismatch leaves the reader to find the rate table."""
    with pytest.raises(ValidationError, match="openai/gpt-oss-120b"):
        _settings(AI_PROVIDER="groq", AI_MODEL="claude-sonnet-5")


@pytest.mark.unit
def test_a_model_and_its_own_provider_are_accepted() -> None:
    """The same pair, one field over — so the check is a pairing and not a ban on Groq."""
    settings = _settings(AI_PROVIDER="groq", AI_MODEL="openai/gpt-oss-120b")

    assert (settings.AI_PROVIDER, settings.AI_MODEL) == ("groq", "openai/gpt-oss-120b")


@pytest.mark.unit
def test_groq_is_still_optional_without_a_key() -> None:
    """Selecting a provider is not the same as enabling AI, and the key stays optional.

    A deployment that sets `AI_PROVIDER=groq` and has not yet obtained a key must still start:
    the API is the thing that serves every other feature, and refusing to boot over an unused
    AI credential would take the whole product down for a subsystem nobody has called. The
    refusal belongs at the first call, which is where `app/ai/groq.py` puts it.
    """
    assert _settings(AI_PROVIDER="groq", AI_MODEL="openai/gpt-oss-120b").AI_API_KEY is None


@pytest.mark.unit
def test_ai_is_optional_and_a_blank_key_is_absent() -> None:
    """A blank key is not a credential, and an absent one is a legal configuration.

    `.env.example` ships `AI_API_KEY=` with no value, so a developer who copies it has an
    empty string in the environment. Without the blank-becomes-`None` rule the AI layer
    would hand the SDK a key that is present and wrong, which fails differently from one
    that is absent and is harder to read. The `None` half is what lets a checkout with no
    key run the entire suite and a self-hosted installation start without AI.
    """
    assert _settings().AI_API_KEY is None
    assert _settings(AI_API_KEY="").AI_API_KEY is None
    assert _settings(AI_API_KEY="   ").AI_API_KEY is None
    assert _settings(AI_API_KEY="sk-ant-real").AI_API_KEY == "sk-ant-real"


@pytest.mark.unit
def test_the_retry_budget_is_what_the_reasoning_argues_for() -> None:
    """Three attempts inside a request, not five over minutes.

    `app/workers/email_tasks.py` retries five times because mail is delivered eventually.
    An AI call has a person watching a spinner, so the whole sequence has to fit inside a
    request: the assertion is that this number never drifts upward by accident.
    """
    settings = _settings()

    assert settings.AI_MAX_ATTEMPTS == 3
    assert settings.AI_TIMEOUT_SECONDS == 30.0


@pytest.mark.unit
@pytest.mark.parametrize(
    "override",
    [
        {"AI_MAX_ATTEMPTS": 0},
        {"AI_TIMEOUT_SECONDS": 0},
        {"AI_RETRY_BACKOFF_SECONDS": -1},
    ],
)
def test_an_ai_setting_that_would_break_the_call_path_is_refused(
    override: dict[str, object],
) -> None:
    """Zero attempts is not a policy — it is a call path that never calls anything.

    Each of these would fail in a way that reads like a bug in the retry loop rather than
    like a misconfiguration, which is why the bound is on the field.
    """
    with pytest.raises(ValidationError):
        _settings(**override)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Embeddings and retrieval (Phase X)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_embedding_provider_defaults_to_the_real_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default is what a deployment gets when nobody decides, and here it is no vendor fallback.

    The mirror of `test_the_ai_provider_defaults_to_the_real_one`, and the reason it is worth a
    line of its own: `openai` is the *only* implementation of §22's protocol, so a default of
    `fake` would leave a deployment that never set the variable embedding its corpus with hashed
    vectors — retrieval that returns results and ranks them meaninglessly.

    The variable is unset rather than assumed absent: conftest pins it, and `_env_file=None`
    suppresses dotenv without touching the process environment. Same pattern as the AI setting's
    test above.
    """
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)

    assert _settings().EMBEDDING_PROVIDER == "openai"


@pytest.mark.unit
def test_an_embedding_model_from_another_vendor_is_refused() -> None:
    """The second pairing, and the reason it is a second pairing.

    `claude-sonnet-5` is a priced model — the check above proves it — so it passes the "do we
    know this rate?" test and then reaches an OpenAI endpoint holding an OpenAI key, which
    rejects a model name it does not serve. `_the_model_is_served_by_the_provider` answers the
    embedding side of the same question with the embedding vendor, and this is the assertion
    that it is a genuinely separate pairing rather than one check reading the wrong fields.

    Explicitly `EMBEDDING_PROVIDER="openai"` because conftest pins `fake`, and the fake's branch
    is skipped by design — a scripted provider serves whatever it is told to.
    """
    with pytest.raises(
        ValidationError, match="is served by 'anthropic', not by EMBEDDING_PROVIDER='openai'"
    ):
        _settings(EMBEDDING_PROVIDER="openai", EMBEDDING_MODEL="claude-sonnet-5")


@pytest.mark.unit
def test_an_unpriced_embedding_model_is_refused() -> None:
    """The same rate check the generation model gets, on the field a deployment got wrong.

    A name from the era before `text-embedding-ada-002` was retired is plausible enough to be
    typed, and without this it would write `cost_usd = 0` for every ingestion — a corpus
    embedded for what the dashboard renders as free.
    """
    with pytest.raises(ValidationError, match="has no published rate"):
        _settings(EMBEDDING_PROVIDER="openai", EMBEDDING_MODEL="text-embedding-ada-002")


@pytest.mark.unit
def test_the_embedding_model_and_its_own_vendor_are_accepted() -> None:
    """The same pair, one field over — so this is a pairing and not a ban on embeddings."""
    settings = _settings(EMBEDDING_PROVIDER="openai", EMBEDDING_MODEL="text-embedding-3-small")

    assert (settings.EMBEDDING_PROVIDER, settings.EMBEDDING_MODEL) == (
        "openai",
        "text-embedding-3-small",
    )


@pytest.mark.unit
def test_the_fake_embedding_provider_is_refused_outside_tests() -> None:
    """§60 applies to the embedding vendor too, which is why one validator covers both.

    A knowledge base answered from hash-derived vectors would be indistinguishable from one
    answered from embeddings — the API returns 200 either way — and the retrieval *ranking*
    would be meaningless. That is exactly the shape of "fake AI results in the final
    implementation" the spec forbids, so the guard is shared rather than repeated.

    `ENVIRONMENT=test` is what the suite runs as, so it is overridden here; the message names
    `EMBEDDING_PROVIDER` because that is the field that failed.
    """
    with pytest.raises(ValidationError, match="EMBEDDING_PROVIDER=fake"):
        _settings(EMBEDDING_PROVIDER="fake", ENVIRONMENT="development")


@pytest.mark.unit
def test_a_blank_embedding_model_falls_back_to_the_default() -> None:
    """`.env.example` ships the key with no value, and empty is "not set", not a model.

    Without this the copied file fails startup with *"'' has no published rate"* — a message
    about a model nobody chose. `_blank_model_is_the_default` is the fix, and this is the field
    that motivated it (the generation model is covered by the same validator).

    The provider is left at the suite's `fake` so this asserts the fallback and nothing else;
    the default model is asserted directly, which is what a deployment copying the template
    reaches once `EMBEDDING_PROVIDER` is its own default.
    """
    assert _settings(EMBEDDING_MODEL="").EMBEDDING_MODEL == "text-embedding-3-small"
    assert _settings(EMBEDDING_MODEL="   ").EMBEDDING_MODEL == "text-embedding-3-small"


@pytest.mark.unit
def test_embeddings_are_optional_and_a_blank_key_is_absent() -> None:
    """The `AI_API_KEY` argument, one field over: a blank key is not a credential.

    Unlike AI, there is no non-fake provider a checkout can fall back to without a key — the
    knowledge routes refuse at the point of use — so the `None` half is what lets the whole
    suite run and a self-hosted installation start with no embeddings configured.
    """
    assert _settings().EMBEDDING_API_KEY is None
    assert _settings(EMBEDDING_API_KEY="").EMBEDDING_API_KEY is None
    assert _settings(EMBEDDING_API_KEY="   ").EMBEDDING_API_KEY is None
    assert _settings(EMBEDDING_API_KEY="sk-proj-real").EMBEDDING_API_KEY == "sk-proj-real"


@pytest.mark.unit
def test_the_retrieval_threshold_is_a_probability() -> None:
    """Cosine similarity is bounded to [-1, 1], and a threshold outside it answers nonsense.

    Above 1, nothing ever clears it and the product refuses every question — silently, since
    the refusal is a well-formed answer. Below 0, a passage that is *anti*-correlated with the
    question is treated as relevant, and it is that passage the model is then asked to ground
    an answer in. Both are configuration mistakes with no error of their own, so both are
    refused where they are written.
    """
    assert _settings(RETRIEVAL_MIN_SIMILARITY="0.5").RETRIEVAL_MIN_SIMILARITY == 0.5

    with pytest.raises(ValidationError):
        _settings(RETRIEVAL_MIN_SIMILARITY="1.5")
    with pytest.raises(ValidationError):
        _settings(RETRIEVAL_MIN_SIMILARITY="-0.1")
