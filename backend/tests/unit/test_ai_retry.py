"""§46's provider failure and retry behaviour — and the ledger every attempt writes.

Spec §46 names four AI cases that must be testable. Two are `test_ai_structured_output.py`'s
(a valid response, a malformed one). The other two are here:

* **Provider failure.** A permanent error does not retry; a transient one does.
* **Retry behaviour.** Attempts are bounded, spaced, and each one is accounted for.

The distinction is the whole design of `app/ai/errors.py`, and the assertion that proves it
is not a return value — it is the provider's **call count**. "Retried twice then succeeded"
and "succeeded" produce the same answer, so only the count can tell them apart. That is why
`FakeProvider.calls` is public.

No test here sleeps: `ai_service._sleep` is replaced, which is why it is a module-level name
rather than a direct `asyncio.sleep` call.
"""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.ai.errors import AIOutputError, AIPermanentError, AITransientError
from app.ai.fake import FakeProvider
from app.ai.pricing import cost_usd
from app.ai.provider import AIRequest
from app.core.config import get_settings
from app.core.exceptions import AIServiceError
from app.core.tenancy import TenantContext
from app.models.enums import AIOperation, UserRole
from app.schemas.ai import SentimentResult
from app.services import ai_service

pytestmark = pytest.mark.unit


class _RecordingSession:
    """A stand-in for `AsyncSession` that keeps what was staged on it.

    `ai_service` never commits and never reads, so `add` is the entire surface it needs.
    Using the real session here would make a retry test require Postgres, and the property
    under test has nothing to do with the database.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    @property
    def ledger(self) -> list[Any]:
        return self.added


def _request() -> AIRequest:
    return AIRequest(
        instruction="Classify the ticket.",
        content="I was charged twice for order 88213.",
        content_label="the customer's message",
        max_tokens=1024,
    )


def _context() -> TenantContext:
    return TenantContext(user_id=uuid4(), organization_id=uuid4(), role=UserRole.ADMIN)


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the sleep between attempts, recording the delays instead of taking them."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(ai_service, "_sleep", fake_sleep)
    return delays


def _use(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> FakeProvider:
    """Point the service at a scripted provider. This is the seam `_provider` exists for."""
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


# ---------------------------------------------------------------------------
# §46 — provider failure
# ---------------------------------------------------------------------------


async def test_a_transient_error_is_retried_and_can_succeed(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    provider = _use(
        monkeypatch,
        FakeProvider(
            AITransientError("the provider rate-limited the request"),
            {"category": "Billing", "subcategory": "Duplicate Charge", "confidence": 0.94},
        ),
    )
    session = _RecordingSession()

    result = await ai_service.classify_ticket(session, _context(), _request())

    assert result.category == "Billing"
    assert provider.calls == 2
    assert len(slept) == 1


async def test_a_permanent_error_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """The count is the assertion — see the module docstring.

    Retrying a refused request re-sends the same refusal, so the only correct behaviour is
    one attempt. A test that asserted on the raised exception alone would pass even if the
    service had tried three times and failed three times.
    """
    provider = _use(
        monkeypatch, FakeProvider(AIPermanentError("the provider rejected the API key"))
    )

    with pytest.raises(AIServiceError):
        await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    assert provider.calls == 1
    assert slept == []


async def test_malformed_output_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """§53 names "repeated AI calls" as waste; the same question gets the same answer.

    A model that answered in prose, or truncated at `max_tokens`, will do it again for the
    same input at the same temperature. The fix is a prompt or a setting, not a second
    invoice.
    """
    provider = _use(monkeypatch, FakeProvider("The customer seems frustrated."))

    with pytest.raises(AIServiceError):
        await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    assert provider.calls == 1
    assert slept == []


async def test_the_attempts_are_bounded(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """A provider that is down stays down; the request still has to return."""
    attempts = get_settings().AI_MAX_ATTEMPTS
    provider = _use(
        monkeypatch,
        FakeProvider(*[AITransientError("the provider could not be reached")] * attempts),
    )

    with pytest.raises(AIServiceError):
        await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    assert provider.calls == attempts
    # One fewer sleep than attempts: nothing waits after the last try.
    assert len(slept) == attempts - 1


async def test_a_transient_error_exhausting_attempts_surfaces_as_a_service_error(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """The three internal types are not the API's error surface — `AIServiceError` is.

    The original is chained as the cause rather than discarded, so the log line an operator
    reads keeps the reason while the client gets one stable code.
    """
    attempts = get_settings().AI_MAX_ATTEMPTS
    _use(
        monkeypatch,
        FakeProvider(
            *[AITransientError("the provider did not answer within the timeout")] * attempts
        ),
    )

    with pytest.raises(AIServiceError) as caught:
        await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    assert isinstance(caught.value.__cause__, AITransientError)


async def test_a_permanent_error_is_raised_at_the_first_attempt(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    _use(monkeypatch, FakeProvider(AIPermanentError("the configured model does not exist")))

    with pytest.raises(AIServiceError) as caught:
        await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    assert isinstance(caught.value.__cause__, AIPermanentError)


# ---------------------------------------------------------------------------
# Retry spacing
# ---------------------------------------------------------------------------


def test_the_backoff_grows_exponentially() -> None:
    """Attempts are spaced 0s, ~1s, ~3s in total — inside a request, out of a rate limiter's way."""
    settings = SimpleNamespace(AI_RETRY_BACKOFF_SECONDS=1.0)

    first = ai_service._backoff_seconds(settings, 1)
    second = ai_service._backoff_seconds(settings, 2)

    assert 0.5 <= first <= 1.0
    assert 1.0 <= second <= 2.0


def test_the_backoff_is_jittered_and_never_collapses_to_zero() -> None:
    """Full jitter would be the textbook choice and is wrong for a rate limit.

    A delay near zero is the one case where waiting is pointless, so the floor is half the
    ceiling. The variance is what stops a hundred throttled workers returning in lockstep.
    """
    settings = SimpleNamespace(AI_RETRY_BACKOFF_SECONDS=1.0)

    seen = {ai_service._backoff_seconds(settings, 1) for _ in range(200)}

    assert len(seen) > 1, "the jitter is not varying"
    assert all(0.5 <= delay <= 1.0 for delay in seen)


def test_a_zero_backoff_configures_no_wait() -> None:
    """`AI_RETRY_BACKOFF_SECONDS = 0` means retry immediately — legal, and honest about it."""
    settings = SimpleNamespace(AI_RETRY_BACKOFF_SECONDS=0.0)

    assert ai_service._backoff_seconds(settings, 3) == 0.0


# ---------------------------------------------------------------------------
# The ledger every attempt writes
# ---------------------------------------------------------------------------


async def test_a_successful_call_writes_one_row(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    _use(monkeypatch, FakeProvider({"category": "Billing", "confidence": 0.94}))
    session = _RecordingSession()
    context = _context()
    ticket_id = uuid4()

    await ai_service.classify_ticket(session, context, _request(), ticket_id=ticket_id)

    assert len(session.ledger) == 1
    row = session.ledger[0]
    assert row.operation is AIOperation.CLASSIFY
    assert row.provider == "fake"
    assert row.model == get_settings().AI_MODEL
    assert row.ticket_id == ticket_id
    assert row.user_id == context.user_id
    assert row.was_successful is True


async def test_the_tenant_comes_from_the_context_and_nowhere_else(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """§4: never trust an `organization_id` supplied by the frontend.

    `_stage_usage` takes a `TenantContext`, so there is no parameter a caller could pass a
    tenant through even if it wanted to — which is the point of taking the context rather
    than an id.
    """
    _use(monkeypatch, FakeProvider({"category": "Billing", "confidence": 0.9}))
    session = _RecordingSession()
    context = _context()

    await ai_service.classify_ticket(session, context, _request())

    assert session.ledger[0].organization_id == context.organization_id


async def test_every_attempt_is_recorded_including_the_failures(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """`AIUsage`: *"A failed call still consumed quota and may still have been billed."*

    A dashboard that folded these rows away would report spend with no way to see how much
    of it bought nothing — which is what `/analytics/overview`'s `failed_calls` is for.
    """
    provider = _use(
        monkeypatch,
        FakeProvider(
            AITransientError("the provider could not be reached"),
            {"category": "Billing", "confidence": 0.9},
        ),
    )

    session = _RecordingSession()
    await ai_service.classify_ticket(session, context=_context(), request=_request())

    assert provider.calls == 2
    assert len(session.ledger) == 2
    assert [row.was_successful for row in session.ledger] == [False, True]


async def test_a_failed_attempt_records_the_tokens_it_was_billed_for(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """A truncated answer is still an invoice.

    This is the case that motivates the token counts riding on the exception at all: the
    answer was generated, `max_tokens` cut it off, and the provider charged for it. Nothing
    returns from that path, so the counts have to travel with the error.
    """
    _use(
        monkeypatch,
        FakeProvider(
            AIOutputError(
                "the answer was truncated at max_tokens",
                prompt_tokens=900,
                completion_tokens=40,
            )
        ),
    )
    session = _RecordingSession()

    with pytest.raises(AIServiceError):
        await ai_service.classify_ticket(session, _context(), _request())

    row = session.ledger[0]
    assert row.was_successful is False
    assert (row.prompt_tokens, row.completion_tokens) == (900, 40)
    assert row.cost_usd > 0


async def test_the_row_is_priced_from_the_model_not_left_at_zero(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """A zero-cost row is the failure `app/ai/pricing.py` exists to prevent."""
    _use(
        monkeypatch,
        FakeProvider(
            {"category": "Billing", "confidence": 0.9},
            prompt_tokens=2_000,
            completion_tokens=500,
        ),
    )
    session = _RecordingSession()

    await ai_service.classify_ticket(session, _context(), _request())

    row = session.ledger[0]
    # 2 000 in at $2/MTok plus 500 out at $10/MTok is $0.009.
    assert str(row.cost_usd) == "0.009000"


async def test_the_row_is_never_marked_cached(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """Phase T has no AI cache; claiming one would corrupt the measurement it exists for."""
    _use(monkeypatch, FakeProvider({"category": "Billing", "confidence": 0.9}))
    session = _RecordingSession()

    await ai_service.classify_ticket(session, _context(), _request())

    assert session.ledger[0].was_cached is False


async def test_each_operation_records_its_own_name(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """`by_operation` on the dashboard is only meaningful if this is right."""
    provider = _use(monkeypatch, FakeProvider({"sentiment": "negative", "confidence": 0.96}))
    session = _RecordingSession()

    result = await ai_service.analyze_sentiment(session, _context(), _request())

    assert isinstance(result, SentimentResult)
    assert session.ledger[0].operation is AIOperation.SENTIMENT
    assert provider.calls == 1


async def test_latency_is_recorded_for_every_attempt(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """Milliseconds, and present on failures too — a slow failure is a fact worth keeping."""
    _use(
        monkeypatch,
        FakeProvider(
            AITransientError("the provider did not answer within the timeout"),
            {"category": "Billing", "confidence": 0.9},
        ),
    )
    session = _RecordingSession()

    await ai_service.classify_ticket(session, _context(), _request())

    for row in session.ledger:
        assert isinstance(row.latency_ms, int)
        assert row.latency_ms >= 0


async def test_the_provider_token_counts_are_what_reaches_the_ledger(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """The counts are the vendor's, passed through — not estimated from the prompt."""
    _use(
        monkeypatch,
        FakeProvider(
            {"category": "Billing", "confidence": 0.9},
            prompt_tokens=1_337,
            completion_tokens=42,
        ),
    )
    session = _RecordingSession()

    await ai_service.classify_ticket(session, _context(), _request())

    row = session.ledger[0]
    assert (row.prompt_tokens, row.completion_tokens) == (1_337, 42)


async def test_the_request_reaches_the_provider_fenced(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """`ai_service` passes the request through; `ClaudeProvider` is what fences it.

    Asserted here as the negative: the service does **not** rewrite the content, so the
    fencing has exactly one implementation, in the provider, where it cannot be forgotten
    or applied twice.
    """
    provider = _use(monkeypatch, FakeProvider({"category": "Billing", "confidence": 0.9}))
    request = _request()

    await ai_service.classify_ticket(_RecordingSession(), _context(), request)

    assert provider.requests == [request]


def test_the_sdk_retries_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ClaudeProvider` turns the SDK's own retries off, so attempts are countable.

    `max_retries=0` is what makes `FakeProvider.calls` — and therefore every assertion in
    this file — a statement about the real path too. If the SDK retried underneath, one
    call could make four HTTP requests while the ledger recorded one, and
    `AI_MAX_ATTEMPTS` would bound nothing.
    """
    from app.ai import claude

    built: dict[str, Any] = {}

    def fake_client(**kwargs: Any) -> SimpleNamespace:
        built.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(claude, "_client", None)
    monkeypatch.setattr(claude, "AsyncAnthropic", fake_client)
    monkeypatch.setattr(
        claude,
        "get_settings",
        lambda: SimpleNamespace(AI_API_KEY="test-key", AI_TIMEOUT_SECONDS=30.0),
    )

    claude._shared_client()

    assert built["max_retries"] == 0
    assert built["timeout"] == 30.0
    assert claude.ClaudeProvider().name == "anthropic"


def test_a_missing_key_fails_at_the_point_of_use_and_not_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AI_API_KEY` is optional in `Settings`, so this is the line that has to refuse.

    The alternative — a required-and-defaultless key — would break every CI checkout and
    the whole test suite, which is the reasoning that made the SMTP credentials optional
    too. The failure is loud here instead, naming the setting to set.
    """
    from app.ai import claude

    monkeypatch.setattr(claude, "_client", None)
    monkeypatch.setattr(
        claude,
        "get_settings",
        lambda: SimpleNamespace(AI_API_KEY=None, AI_TIMEOUT_SECONDS=30.0),
    )

    with pytest.raises(AIPermanentError, match="AI_API_KEY is not configured"):
        claude._shared_client()


# ---------------------------------------------------------------------------
# The billed tokens on the one failure path that does not happen in the SDK
# ---------------------------------------------------------------------------


def _claude_saying(monkeypatch: pytest.MonkeyPatch, response: Any) -> Any:
    """Point `ClaudeProvider` at a client that returns `response` for every call.

    The provider is the real one and `ai_service` is the real one; only the socket is
    gone. That is what makes the assertions below about `app/ai/` rather than about a
    double: `_structured`'s own handling of the response is what is under test.
    """
    from app.ai import claude

    async def create(**kwargs: Any) -> Any:
        return response

    monkeypatch.setattr(claude, "_client", None)
    monkeypatch.setattr(
        claude,
        "AsyncAnthropic",
        lambda **kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    monkeypatch.setattr(
        claude,
        "get_settings",
        lambda: SimpleNamespace(
            AI_API_KEY="test-key",
            AI_MODEL="claude-sonnet-5",
            AI_TIMEOUT_SECONDS=30.0,
        ),
    )
    monkeypatch.setattr(ai_service, "_provider", claude.ClaudeProvider)
    return claude


def _tool_use(payload: object, *, input_tokens: int, output_tokens: int) -> Any:
    """A `Message` as far as `_structured` reads it: usage, stop reason, and one block."""
    from anthropic.types import ToolUseBlock

    return SimpleNamespace(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                type="tool_use", id="toolu_test", name="record_classification", input=payload
            )
        ],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


async def test_a_rejected_answer_is_priced_from_the_tokens_it_was_billed(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """The most expensive failure there is must not reach the ledger at zero cost.

    A model that answers at length and then does not match the schema has spent every one
    of those output tokens. `validate_output` is shared with `FakeProvider` and is handed
    a payload with no usage attached, so the counts exist only inside `_structured` — and
    if they are dropped there, `ai_service` prices the row from tokens it was never told
    about and writes `cost_usd = 0`. A dashboard renders that as *free*, which is a wrong
    number rather than a missing one, and it is invisible in a suite that only ever
    scripts `AIOutputError` directly.
    """
    payload = {"category": "Billing", "confidence": "very sure"}
    _claude_saying(monkeypatch, _tool_use(payload, input_tokens=4_242, output_tokens=311))
    session = _RecordingSession()

    with pytest.raises(AIServiceError) as caught:
        await ai_service.classify_ticket(session, _context(), _request())

    assert isinstance(caught.value.__cause__, AIOutputError)
    row = session.ledger[0]
    assert row.was_successful is False
    assert (row.prompt_tokens, row.completion_tokens) == (4_242, 311)
    assert row.cost_usd == cost_usd("claude-sonnet-5", prompt_tokens=4_242, completion_tokens=311)
    assert row.cost_usd > 0
    # One attempt: a payload the schema refuses is refused identically a second time.
    assert len(session.ledger) == 1


async def test_a_response_without_a_tool_call_is_an_output_error(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """`tool_choice` named one tool, so a plain answer is a malformed one.

    Cheap to reach and worth pinning, because the failure it guards is a silent one: an
    implementation that fell back to reading `response.content[0].text` would appear to
    work against a model that happened to answer in the right shape.
    """
    _claude_saying(
        monkeypatch,
        SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="The customer is upset.")],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        ),
    )
    session = _RecordingSession()

    with pytest.raises(AIServiceError) as caught:
        await ai_service.classify_ticket(session, _context(), _request())

    cause = caught.value.__cause__
    assert isinstance(cause, AIOutputError)
    assert "without a tool call" in cause.reason
    assert (cause.prompt_tokens, cause.completion_tokens) == (100, 20)
