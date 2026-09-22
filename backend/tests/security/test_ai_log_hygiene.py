"""The AI layer's secrets and its subjects: what must not appear in a log line.

Spec §4 forbids tokens in logs, §54 repeats it, and the AI layer is where the pressure is
highest — a provider SDK's exceptions are unusually informative, and the cheapest way to
debug a failing call is to print it. `app/workers/email_tasks.py` wrote the rule down for
its own vendor: log `error_type=type(exc).__name__`, never `str(exc)`, because *"an SDK
error can echo request headers, and a request header carries the key."*

**Three things must not reach a log line, and they are different kinds of thing.**

1. **The API key.** It is a credential, and it is the one value here whose leak is a
   billing and abuse incident rather than an embarrassment.
2. **The assembled prompt.** It contains the fence markers, the preamble, and the whole of
   the customer's text — and it is the value a developer is most tempted to add to a log
   line while tuning a prompt.
3. **The customer's own words.** §4's tenant isolation and §20's data handling both mean a
   ticket body is not log payload. A support desk's logs are read by operators, shipped to
   a log aggregator, and retained; the ticket belongs in the ticket.

**The errors below are constructed to be worse than the real ones.** A live SDK error
carries a status code and a parsed body, and Anthropic's own 401 does not quote the key —
but the application must not *depend* on the vendor redacting, and a proxy in front of the
API is exactly the kind of component that echoes a header into a body. So each failure here
is built with the key deliberately inside its message, and the assertion is that none of it
survives `_translate` and `_structured`'s logging.

**The recorder is the one `tests/security/test_log_hygiene.py` defines**, imported rather
than copied: there is one implementation of "record what was logged", and a second one here
would drift from the first. `LOGGING_MODULES` over there is the registry of modules that
hold a logger, and it names the two below.

**No database and no HTTP.** Every call goes through `ai_service` with a real
`ClaudeProvider` and a stand-in for the socket, so the two modules under watch are the real
ones and the assertions are about the real path.
"""

import inspect
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from anthropic import APIConnectionError, AuthenticationError
from anthropic.types import ToolUseBlock

from app.ai import claude
from app.ai.provider import AIRequest
from app.core.exceptions import AIServiceError
from app.core.tenancy import TenantContext
from app.models.enums import UserRole
from app.services import ai_service
from tests.security.test_log_hygiene import LogRecorder

pytestmark = pytest.mark.security

# A distinctive credential. Recognisable in a failure message, and long enough that a
# substring match cannot happen by accident — which matters, because "the key was not in
# the log" is only meaningful if the search string is unique.
API_KEY = "sk-ant-api03-this-key-must-never-be-logged-7f3c91"

# The ticket, as a customer wrote it, and as it reaches the prompt: subject first, then the
# body, which is the shape `Ticket.description` takes in every prompt Phases U-W will
# assemble. `BODY` is long enough that a truncated log line still contains most of it, so a
# leak that logged the first hundred characters would still fail here.
SUBJECT = "Duplicate charge on invoice 88213"
BODY = (
    "I was charged twice for order 88213 on the 14th and nobody has replied to my last "
    "three emails. My card ends 4242 and I would like the second charge refunded today."
)
CONTENT = f"{SUBJECT}\n\n{BODY}"

# The four log lines the AI layer emits. Asserted present, so a rename shows up as a
# failure here rather than as a silently vacuous "nothing was logged, so nothing leaked".
EXPECTED_EVENTS = frozenset(
    {"ai_provider_error", "ai_call_retrying", "ai_call_failed", "ai_call_succeeded"}
)

# The modules that hold a logger on the AI path. Listed explicitly, following
# `test_log_hygiene.py`, so that a third module growing one is a deliberate addition.
AI_LOGGING_MODULES = ("app.ai.claude", "app.services.ai_service")

# A tool response the schema accepts, so the success path is reachable without a network.
VALID_CLASSIFICATION = {
    "category": "Billing",
    "subcategory": "Duplicate Charge",
    "confidence": 0.94,
}


class _RecordingSession:
    """A stand-in for `AsyncSession`; `ai_service` only ever calls `add` on it."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, instance: Any) -> None:
        self.added.append(instance)


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    """Replace both AI loggers with one recorder."""
    recorder = LogRecorder()
    for module in AI_LOGGING_MODULES:
        monkeypatch.setattr(f"{module}.logger", recorder)
    return recorder


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries happen, the delays do not — a log-hygiene test should not take seconds."""

    async def immediately(seconds: float) -> None:
        return None

    monkeypatch.setattr(ai_service, "_sleep", immediately)


def _context() -> TenantContext:
    return TenantContext(user_id=uuid4(), organization_id=uuid4(), role=UserRole.ADMIN)


def _request() -> AIRequest:
    return AIRequest(
        instruction="Classify this ticket into a category.",
        content=CONTENT,
        content_label="the customer's message",
        max_tokens=1024,
    )


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raises: Exception | None = None,
    block_input: Any = None,
    seen: dict[str, Any] | None = None,
) -> None:
    """Point `ClaudeProvider` at a stand-in socket, answering with one of two behaviours.

    `_client` is reset because `_shared_client` caches process-wide, and `get_settings` is
    replaced so the key under test is the one the client would be built with — the
    credential has to be *in play* for its absence from the log to mean anything.
    """

    async def create(**kwargs: Any) -> Any:
        if seen is not None:
            seen.update(kwargs)
        if raises is not None:
            raise raises
        return SimpleNamespace(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    type="tool_use",
                    id="toolu_test",
                    name="record_classification",
                    input=block_input,
                )
            ],
            usage=SimpleNamespace(input_tokens=900, output_tokens=60),
        )

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
            AI_API_KEY=API_KEY, AI_MODEL="claude-sonnet-5", AI_TIMEOUT_SECONDS=30.0
        ),
    )
    monkeypatch.setattr(ai_service, "_provider", claude.ClaudeProvider)


def _assert_clean(logs: LogRecorder) -> None:
    """The one assertion every test in this file makes, over every secret in play."""
    text = logs.as_text()
    for secret, name in (
        (API_KEY, "the API key"),
        (SUBJECT, "the customer's subject"),
        (BODY, "the customer's message"),
    ):
        assert secret not in text, f"{name} reached a log line:\n{text}"


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------


async def test_a_transient_failure_logs_the_reason_and_never_the_credentials(
    logs: LogRecorder, no_waiting: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure that retries, so it logs three times, and every one must be clean.

    `APIConnectionError` is built with the key inside its message, which is the leak this
    rule exists to stop. What is logged instead is the exception's *type* and the
    translated *reason* — both constants, neither of them containing a credential.
    """
    _install_client(
        monkeypatch,
        raises=APIConnectionError(
            message=f"Connection error: request header x-api-key={API_KEY} was rejected",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        ),
    )

    with pytest.raises(AIServiceError):
        await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    _assert_clean(logs)
    assert {"ai_provider_error", "ai_call_retrying", "ai_call_failed"} <= logs.events
    # No *fifth* event name. The four above are the whole vocabulary, and a new one is a
    # new set of fields — which is how a prompt or a body gets into a log line: not by
    # editing an existing call, but by adding a helpful new one beside it.
    assert logs.events <= EXPECTED_EVENTS


async def test_a_rejected_key_is_named_by_its_status_and_not_by_its_value(
    logs: LogRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 is the loudest path in the layer, and the first line an operator reads.

    What they need is *"the provider rejected the API key"* — `_STATUS_REASONS`' fixed
    sentence for 401 — plus the exception type. What they must not get is the body the
    provider returned, because a body is vendor-controlled text that a proxy or a
    misconfiguration can fill with anything, including the header it just received.
    """
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    _install_client(
        monkeypatch,
        raises=AuthenticationError(
            f"Error code: 401 - invalid x-api-key: {API_KEY}",
            response=httpx.Response(401, request=request),
            body=None,
        ),
    )

    with pytest.raises(AIServiceError):
        await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    _assert_clean(logs)
    text = logs.as_text()
    assert "the provider rejected the API key" in text
    # The type, not the instance: a reviewer should be able to tell which failure happened
    # without reading the vendor's prose. One attempt, too — a 401 does not retry.
    assert "AuthenticationError" in text
    assert "ai_call_retrying" not in logs.events


# ---------------------------------------------------------------------------
# The ticket
# ---------------------------------------------------------------------------


async def test_a_successful_call_does_not_log_the_ticket_it_read(
    logs: LogRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success path logs counts, ids, and a duration — never the subject or the body.

    The positive control lives inside the same test as the assertion, because "no secret
    was logged" and "nothing was logged at all" are otherwise the same result.
    """
    _install_client(monkeypatch, block_input=VALID_CLASSIFICATION)

    await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    _assert_clean(logs)
    assert "ai_call_succeeded" in logs.events
    assert logs.events <= EXPECTED_EVENTS


async def test_a_rejected_payload_is_summarised_and_never_quoted(
    logs: LogRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AIOutputError`'s reason names the *field* that failed, and nothing that was in it.

    The model is made to answer with the customer's message where a confidence belongs —
    the shape an injected answer takes, text where a number should be. The reason says
    `confidence: float_parsing`; the text itself stops at `validate_output`.
    """
    _install_client(monkeypatch, block_input={"category": "Billing", "confidence": BODY})

    with pytest.raises(AIServiceError):
        await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    _assert_clean(logs)
    text = logs.as_text()
    assert "ai_call_failed" in logs.events
    assert "AIOutputError" in text
    assert "confidence" in text


async def test_the_prompt_is_assembled_and_never_logged(
    logs: LogRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same claim, asserted from the provider's side.

    The fence is a debugging temptation — *"is the model even seeing my fence?"* — and the
    answer is a unit test of `app/ai/prompts.py`, not an `info` line carrying a customer's
    ticket. This asserts the assembled string reached the provider, and that neither the
    markers nor the ticket behind them appear in the log.
    """
    seen: dict[str, Any] = {}
    _install_client(monkeypatch, block_input=VALID_CLASSIFICATION, seen=seen)

    await ai_service.classify_ticket(_RecordingSession(), _context(), _request())

    # The prompt was built, fenced, and sent...
    sent = seen["messages"][0]["content"]
    assert SUBJECT in sent and BODY in sent
    assert "<<<UNTRUSTED INPUT>>>" in sent
    # ...and the only thing about it that reaches the log is nothing at all.
    _assert_clean(logs)
    assert "UNTRUSTED" not in logs.as_text()


# ---------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------


def test_the_recorder_actually_captured_the_session(logs: LogRecorder) -> None:
    """The positive control `test_log_hygiene.py` establishes, for the same reason.

    A recorder that captured nothing would satisfy every assertion above while proving
    nothing, and the failure mode is quiet: a module path that stopped resolving, or a
    logger that moved out of the module, would turn this file into a suite of vacuous
    passes.
    """
    claude.logger.warning("ai_provider_error", reason="a placeholder")

    assert "ai_provider_error" in logs.events


def test_every_ai_module_that_holds_a_logger_is_watched() -> None:
    """The list is only as good as its completeness, so it is checked against the source.

    `AI_LOGGING_MODULES` is hand-written — it has to be, since the point is a deliberate
    addition rather than discovery — but a hand-written list that has fallen behind is
    worse than none, because it reads as coverage. This reads the two modules' own source
    and fails if one of them logs without being on the list.
    """
    for module in (claude, ai_service):
        assert "logger." in inspect.getsource(module), (
            f"{module.__name__} no longer logs — remove it from AI_LOGGING_MODULES"
        )
        assert module.__name__ in AI_LOGGING_MODULES
