"""Groq's translation of the four operations — tested against a stand-in socket.

ADR-028's claim is that a second vendor costs one module and no new machinery. This file is
where that claim is checked rather than asserted, and the shape of it is the evidence: there
is no fake Groq SDK to write, because the "SDK" is HTTP. `httpx.MockTransport` answers with
bytes we choose, so every case below — a truncation, a schema mismatch, a 429, a body that is
not JSON at all — is reachable without a network, and the whole file runs in the ordinary
suite.

**What is not retested here.** `validate_output`, the retry policy, the ledger, and the tool
envelope's own behaviour are `app/ai/provider.py`'s and `app/services/ai_service.py`'s, and
they are covered by `test_ai_structured_output.py` and `test_ai_retry.py` against the fake.
Re-testing them through Groq would prove that a shared function works twice. What is
tested here is the part that is *only* Groq: which status and `error.code` become which of
our three error types, and whether the fields this vendor happens to use are the ones read.

**The last group is the one worth reading.** A `tool_use_failed` body carries
`failed_generation` — the model's own attempt, derived from a customer's message — so the
reason string has to come from a code we named rather than from the body we were handed.
"""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.ai import groq
from app.ai.errors import AIOutputError, AIPermanentError, AITransientError
from app.ai.provider import AIRequest
from app.schemas.ai import Classification

pytestmark = pytest.mark.unit

MODEL = "openai/gpt-oss-120b"

#: The credential, so that its *absence* from a log line or a reason is observable rather
#: than vacuous. Same reasoning as `test_ai_log_hygiene.py`'s constant.
API_KEY = "gsk_this-key-must-never-be-logged-4c81"

#: §18's own example, as Groq returns it: a JSON **string** under `function.arguments`, which
#: is the OpenAI shape and the one `validate_output` has accepted since Phase T.
CLASSIFICATION_ARGUMENTS = (
    '{"category": "Billing", "subcategory": "Duplicate charge", "confidence": 0.97}'
)


def _request() -> AIRequest:
    return AIRequest(
        instruction="Classify this ticket into a category.",
        content="Duplicate charge on invoice 88213",
        content_label="the customer's message",
        max_tokens=1024,
    )


def _completion(
    *,
    arguments: str | None = CLASSIFICATION_ARGUMENTS,
    finish_reason: str = "tool_calls",
    name: str = "record_classification",
    prompt_tokens: int = 179,
    completion_tokens: int = 129,
) -> dict[str, Any]:
    """A response body in the shape Groq sends — trimmed to the fields this module reads."""
    message: dict[str, Any] = {"role": "assistant", "content": None}
    if arguments is not None:
        message["tool_calls"] = [
            {
                "id": "call_test",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ]
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class Wire:
    """A stand-in socket: what the test wants Groq to answer, and what it was asked.

    One scripted outcome per test rather than a queue, because nothing here retries — the
    retry loop is `ai_service`'s and `test_ai_retry.py`'s, and a provider that retried would
    be the defect those tests exist to catch.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self._payload: dict[str, Any] | None = None
        self._raw: str | None = None
        self._status_code = 200
        self._raises: Exception | None = None

    def answers(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        self._payload, self._status_code = payload, status_code

    def answers_raw(self, text: str, *, status_code: int = 200) -> None:
        """Answer with a body that is not JSON, which `json=` cannot express."""
        self._raw, self._status_code = text, status_code

    def fails(self, exc: Exception) -> None:
        self._raises = exc

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.headers.append(request.headers)
        self.bodies.append(json.loads(request.content))
        if self._raises is not None:
            raise self._raises
        if self._raw is not None:
            return httpx.Response(self._status_code, text=self._raw, request=request)
        assert self._payload is not None, "the test scripted no response"
        return httpx.Response(self._status_code, json=self._payload, request=request)


@pytest.fixture
async def wire(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Wire]:
    """Point `GroqProvider` at a `MockTransport`, with a key in play.

    `_client` is replaced rather than `httpx.AsyncClient`, so the module's own lazy-singleton
    logic is bypassed and the request under test is the one `_structured` actually builds —
    a client built here would be a client the module never made.
    """
    socket = Wire()
    client = httpx.AsyncClient(transport=httpx.MockTransport(socket.handle))
    monkeypatch.setattr(groq, "_client", client)
    monkeypatch.setattr(
        groq,
        "get_settings",
        lambda: SimpleNamespace(AI_API_KEY=API_KEY, AI_MODEL=MODEL, AI_TIMEOUT_SECONDS=30.0),
    )
    try:
        yield socket
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# A valid answer, and what was sent to get it
# ---------------------------------------------------------------------------


async def test_a_tool_call_becomes_a_validated_classification(wire: Wire) -> None:
    """The success path, end to end, with the numbers the ledger will record."""
    wire.answers(_completion())

    result = await groq.GroqProvider().classify_ticket(_request())

    assert isinstance(result.value, Classification)
    assert result.value.category == "Billing"
    assert result.value.subcategory == "Duplicate charge"
    assert result.value.confidence == 0.97
    assert (result.prompt_tokens, result.completion_tokens) == (179, 129)


async def test_arguments_are_a_json_string_rather_than_an_object(wire: Wire) -> None:
    """**The property the whole migration rests on.**

    OpenAI-compatible APIs return tool arguments as text. `validate_output` has parsed text
    since Phase T — it was written that way so `FakeProvider` could script an answer — and
    this asserts that the shape genuinely reaches it, because a change that started
    `json.loads`-ing in this module instead would be §18 implemented a second time.
    """
    wire.answers(_completion(arguments='{"category": "Technical", "confidence": 0.5}'))

    result = await groq.GroqProvider().classify_ticket(_request())

    assert result.value.category == "Technical"


async def test_the_request_is_a_forced_tool_call_with_a_bounded_answer(wire: Wire) -> None:
    """What `_structured` puts on the wire, asserted once rather than assumed everywhere.

    Each field is here for a reason worth naming: `max_completion_tokens` is this vendor's
    name for the ceiling, `reasoning_effort` keeps a reasoning model from spending it on
    thinking, and `tool_choice` is what makes a paragraph-shaped answer impossible rather
    than merely discouraged.
    """
    wire.answers(_completion())

    await groq.GroqProvider().classify_ticket(_request())

    body = wire.bodies[0]
    assert body["model"] == MODEL
    assert body["max_completion_tokens"] == 1024
    assert body["reasoning_effort"] == "low"
    assert body["tool_choice"] == {
        "type": "function",
        "function": {"name": "record_classification"},
    }
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "record_classification"
    # `additionalProperties: false` is `_STRICT`'s doing in `app/schemas/ai.py`, and seeing it
    # here is the proof that the schema this module sends is the shared one.
    assert body["tools"][0]["function"]["parameters"]["additionalProperties"] is False
    assert body["tools"][0]["function"]["parameters"]["properties"]["confidence"]["maximum"] == 1


async def test_the_customer_text_is_fenced_and_the_instruction_is_a_system_message(
    wire: Wire,
) -> None:
    """§4 and `app/ai/prompts.py`: customer text is data, and it is labelled as such.

    The split matters as much as the fence. The instruction is a *system* message and the
    ticket is a *user* message, so a customer who writes something shaped like an instruction
    is describing a thing that happened rather than issuing one.
    """
    wire.answers(_completion())

    await groq.GroqProvider().classify_ticket(_request())

    system, user = wire.bodies[0]["messages"]
    assert system == {
        "role": "system",
        "content": "Classify this ticket into a category.",
    }
    assert user["role"] == "user"
    assert "<<<UNTRUSTED INPUT>>>" in user["content"]
    assert "<<<END UNTRUSTED INPUT>>>" in user["content"]
    assert "Duplicate charge on invoice 88213" in user["content"]


async def test_the_key_is_a_header_and_never_part_of_the_request_body(wire: Wire) -> None:
    """§54: a credential that is not in the body is a credential a body-log cannot leak.

    Asserted on both halves. The header check is the whole mechanism — one place in the module
    touches the key, and it is the client's construction — and the body check is what makes
    "the key never reaches a log line" a property of the request rather than of the logger.
    """
    wire.answers(_completion())

    await groq.GroqProvider().classify_ticket(_request())

    assert wire.requests[0].url == httpx.URL(f"{groq._API_ROOT}/chat/completions")
    assert API_KEY not in json.dumps(wire.bodies[0])


# ---------------------------------------------------------------------------
# Answers we refuse — each one, the tokens are billed whether or not we keep the value
# ---------------------------------------------------------------------------


async def test_a_truncated_answer_carries_its_tokens_into_the_ledger(wire: Wire) -> None:
    """`length` means the tool arguments are half-written, and the fix is a bigger ceiling.

    The counts are the point of the assertion. A truncation is the most expensive way to get
    nothing — the model generated right up to the limit — so a failure that reached the ledger
    as a zero-cost row would be the wrong number rather than a missing one.
    """
    wire.answers(_completion(arguments='{"category": "Bi', finish_reason="length"))

    with pytest.raises(AIOutputError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert "max_completion_tokens" in caught.value.reason
    assert (caught.value.prompt_tokens, caught.value.completion_tokens) == (179, 129)


async def test_an_answer_with_no_tool_call_is_refused(wire: Wire) -> None:
    """A prose answer, which `tool_choice` was supposed to make impossible."""
    wire.answers(_completion(arguments=None, finish_reason="stop"))

    with pytest.raises(AIOutputError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert "without a tool call" in caught.value.reason
    assert caught.value.prompt_tokens == 179


async def test_a_call_to_another_tool_is_refused(wire: Wire) -> None:
    """The model went off-script in a way that is a prompt problem rather than a schema one."""
    wire.answers(_completion(name="record_sentiment"))

    with pytest.raises(AIOutputError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert "not the one it was told to use" in caught.value.reason


async def test_a_content_filter_is_permanent_and_does_not_burn_a_retry(wire: Wire) -> None:
    """The provider declined. The same words produce the same verdict, so retrying is waste."""
    wire.answers(_completion(arguments=None, finish_reason="content_filter"))

    with pytest.raises(AIPermanentError, match="filtered"):
        await groq.GroqProvider().classify_ticket(_request())


async def test_arguments_that_break_the_schema_are_refused_with_their_tokens(wire: Wire) -> None:
    """**The fix Phase T needed in `claude.py`, repeated here for the same reason.**

    A generated answer that fails validation was billed exactly like one that passed. The
    re-attachment lives in `_structured`'s `except AIOutputError`, and this test is the only
    thing that observes it — without it the ledger would price this as free.
    """
    wire.answers(_completion(arguments='{"category": "Billing", "confidence": 1.4}'))

    with pytest.raises(AIOutputError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert "Classification" in caught.value.reason
    assert (caught.value.prompt_tokens, caught.value.completion_tokens) == (179, 129)


async def test_an_invented_field_is_refused_rather_than_dropped(wire: Wire) -> None:
    """`extra="forbid"`: a field the model made up is not silently discarded."""
    wire.answers(
        _completion(arguments='{"category": "Billing", "confidence": 0.9, "urgency": "high"}')
    )

    with pytest.raises(AIOutputError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert "urgency" in caught.value.reason


async def test_a_body_that_is_not_json_is_refused(wire: Wire) -> None:
    """A 200 whose body is unreadable — a proxy's error page, say.

    There is no usage block to read, so the failure prices at zero. That is the honest number
    rather than a missing one: the provider told us nothing about what it billed, and a
    guess would be indistinguishable from a measurement.
    """
    wire.answers_raw("<html>gateway</html>")

    with pytest.raises(AIOutputError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert caught.value.reason == "the response was not JSON"
    assert caught.value.prompt_tokens == 0


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


def _status_error(status_code: int, *, body: dict[str, Any] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"{groq._API_ROOT}/chat/completions")
    return httpx.HTTPStatusError(
        "boom",
        request=request,
        response=httpx.Response(status_code, json=body or {}, request=request),
    )


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 502, 503])
def test_statuses_worth_another_attempt_are_transient(status_code: int) -> None:
    """The same set `claude.py` retries on, reached through a different transport."""
    assert isinstance(groq._from_status(_status_error(status_code)), AITransientError)


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 413, 422])
def test_statuses_that_would_repeat_are_permanent(status_code: int) -> None:
    """A bad key is not improved by sending it again three times."""
    assert isinstance(groq._from_status(_status_error(status_code)), AIPermanentError)


def test_an_unknown_status_gets_a_wording_rather_than_a_key_error() -> None:
    """A provider inventing a status code is reported, not crashed on."""
    assert groq._from_status(_status_error(418)).reason == "the provider returned an error"


def test_a_timeout_is_transient_and_is_not_reported_as_unreachable() -> None:
    """`TimeoutException` is a `TransportError`'s subclass, so the check order is load-bearing.

    Read the other way round, every timeout would be translated as "could not be reached" —
    a different failure with a different fix, and one that sends its reader to look at DNS.
    """
    translated = groq._translate(httpx.ReadTimeout("slow"))

    assert isinstance(translated, AITransientError)
    assert translated.reason == "the provider did not answer within the timeout"


def test_a_connection_failure_is_transient() -> None:
    assert isinstance(groq._translate(httpx.ConnectError("refused")), AITransientError)


def test_an_unrecognised_http_error_is_permanent() -> None:
    """The safe direction: one attempt, rather than three for a request that cannot work."""
    assert isinstance(groq._translate(httpx.HTTPError("odd")), AIPermanentError)


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------


async def test_a_missing_key_is_a_permanent_error_at_the_point_of_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AI_API_KEY` is optional in `Settings`, so this is the line that has to refuse.

    Permanent rather than transient, because all three attempts would be made with the same
    absent key — and at the point of use rather than at import, so a checkout with no key
    still runs the entire suite.
    """
    monkeypatch.setattr(groq, "_client", None)
    monkeypatch.setattr(
        groq,
        "get_settings",
        lambda: SimpleNamespace(AI_API_KEY=None, AI_MODEL=MODEL, AI_TIMEOUT_SECONDS=30.0),
    )

    with pytest.raises(AIPermanentError, match="AI_API_KEY is not configured"):
        await groq.GroqProvider().classify_ticket(_request())


# ---------------------------------------------------------------------------
# What a failed call is allowed to say
# ---------------------------------------------------------------------------


async def test_a_malformed_generation_is_an_output_error_and_not_a_400(
    wire: Wire,
) -> None:
    """**The vendor behaviour an OpenAI-compatible client gets wrong.**

    Groq reports a tool call the schema rejected as HTTP 400 with `code: tool_use_failed`.
    A 400 is permanent-and-not-our-fault; this is *our answer was unusable*, which is a
    different condition with a different type. Filing it by status alone would report a
    schema mismatch as a provider rejecting the request, and send its reader to look at the
    request body.
    """
    wire.answers(
        {
            "error": {
                "message": "Failed to call a function.",
                "type": "invalid_request_error",
                "code": "tool_use_failed",
                "failed_generation": "<|constrain|>Classification",
            }
        },
        status_code=400,
    )

    with pytest.raises(AIOutputError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert caught.value.reason == "the model produced a tool call that did not match the schema"


async def test_the_failed_generation_never_reaches_the_reason(wire: Wire) -> None:
    """§54 and §18, applied to text we *receive* rather than text we send.

    `failed_generation` is the model's own attempt, built from a customer's message, and it
    can therefore contain that message. The reason comes from an allowlist of codes this
    module named — the discipline `_STATUS_REASONS` established one vendor over — so the body
    is never read into a string that reaches a log line.
    """
    echo = "Duplicate charge on invoice 88213 and my card ends 4242"
    wire.answers(
        {
            "error": {
                "message": f"Failed to call a function. Output: {echo}",
                "type": "invalid_request_error",
                "code": "tool_use_failed",
                "failed_generation": echo,
            }
        },
        status_code=400,
    )

    with pytest.raises(AIOutputError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert echo not in caught.value.reason
    assert "88213" not in caught.value.reason


async def test_a_known_error_code_is_named_and_an_unknown_one_is_not(wire: Wire) -> None:
    """A code we listed gets its wording; a code we did not falls back to the status.

    The second half is the point: a passthrough would forward a `message` on the day this
    vendor adds an error type, which is the day nobody is reading the diff.
    """
    wire.answers(
        {"error": {"message": "slow down now please", "code": "rate_limit_exceeded"}},
        status_code=429,
    )
    with pytest.raises(AITransientError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert caught.value.reason == "the provider rate-limited the request"
    assert "slow down now please" not in caught.value.reason

    wire.answers(
        {"error": {"message": "something new", "code": "brand_new_thing"}},
        status_code=400,
    )
    with pytest.raises(AIPermanentError) as unknown:
        await groq.GroqProvider().classify_ticket(_request())

    assert unknown.value.reason == "the provider rejected the request"


async def test_a_401_is_permanent_so_a_wrong_key_costs_one_attempt(wire: Wire) -> None:
    """The walkthrough's deliberate failure, at the unit level."""
    wire.answers(
        {"error": {"message": "Invalid API Key", "code": "invalid_api_key"}}, status_code=401
    )

    with pytest.raises(AIPermanentError) as caught:
        await groq.GroqProvider().classify_ticket(_request())

    assert caught.value.reason == "the provider rejected the API key"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_the_provider_is_chosen_by_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AI_PROVIDER=groq` reaches this module, and `ai_service` does not know which vendor it
    got — which is the whole of what the interface promised."""
    from app.services import ai_service

    monkeypatch.setattr(ai_service, "get_settings", lambda: SimpleNamespace(AI_PROVIDER="groq"))

    assert isinstance(ai_service._provider(), groq.GroqProvider)


def test_the_shared_client_carries_the_key_as_a_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one place in this module that touches the credential, asserted directly.

    Built once and reused, like `claude.py`'s — a per-call client would repeat a TLS
    handshake whose cost is real even beside a call dominated by the model thinking — and the
    key goes in a default header so no request-building path can ever carry it.
    """
    monkeypatch.setattr(groq, "_client", None)
    monkeypatch.setattr(
        groq,
        "get_settings",
        lambda: SimpleNamespace(AI_API_KEY=API_KEY, AI_TIMEOUT_SECONDS=30.0),
    )

    client = groq._shared_client()

    assert client.headers["authorization"] == f"Bearer {API_KEY}"
    assert groq._shared_client() is client
