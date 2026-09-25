"""Groq — the second vendor, and the evidence that the first one's boundary was real.

ADR-008 chose Claude behind `AIProvider`, and Phase T built the interface with one
implementation behind it. **An interface with one implementation is a hypothesis.** This
module is the test of it, and what it cost to write is the result: no change to §17's
protocol, no change to `validate_output`, no change to the retry policy, no change to the
ledger, and no new error type. `ai_service` selects a provider by name and does not know
which one it got; `client.py` and this module are the only two files in the project that
import a vendor.

**The one thing that made it cheap is `validate_output` accepting a JSON string.** It was
written that way for `FakeProvider` — a test double that scripts an answer as text — and it
turns out to be exactly the shape an OpenAI-compatible tool call returns: not an object, but
`tool_calls[0].function.arguments` as a *string* of JSON. §18's guarantee crossed vendors
without a line changing, which is the strongest available evidence that it was implemented at
the right layer.

**Three Groq behaviours that an OpenAI-compatible client gets wrong by assuming sameness.**
They are the reason this module is not a thin alias:

1. **A malformed generation is an HTTP 400.** `error.code == "tool_use_failed"` means the
   model produced a tool call the schema rejected — *our answer is unusable*, which is
   `AIOutputError`, not the permanent-not-our-fault condition a 400 normally signals. Mapping
   it by status code alone would file it under the wrong type and lose the distinction the
   ledger's failure vocabulary exists to keep.
2. **`gpt-oss-120b` is a reasoning model, and its reasoning tokens are inside the ceiling.**
   Left at the default effort it can spend `AI_MAX_TOKENS` thinking and truncate before it
   ever calls the tool, which surfaces as `finish_reason: "length"` and a priced failure. So
   the effort is set low: these four operations are narrow extractions whose shape the tool
   schema already fixes, and there is nothing here for the model to deliberate about.
3. **The error body can contain the customer's own words.** A `tool_use_failed` body carries
   `failed_generation` — the model's attempt, derived from the prompt. So no reason string in
   this module is ever built from `error.message` or the raw body; they come from an allowlist
   of known `error.code` values, the discipline `_STATUS_REASONS` already established one
   vendor over. §54's "no tokens in logs" applies to text we receive, not only text we send.

**`Retry-After` is deliberately ignored.** It is Groq telling us how long *it* wants to be
left alone, and on a free tier it is routinely longer than the entire budget our three
attempts must fit inside — a request a support agent is watching a spinner for. Honouring it
would turn a fast, honest failure into a hung request. The failure is translated as transient
and `ai_service` retries on its own schedule, which is the one place that decision belongs.
"""

from collections.abc import Mapping
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from app.ai.errors import AIError, AIOutputError, AIPermanentError, AITransientError
from app.ai.prompts import as_untrusted
from app.ai.provider import AIRequest, AIResult, _tool_schema, validate_output
from app.core.config import get_settings
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)

logger = structlog.get_logger(__name__)

_API_ROOT = "https://api.groq.com/openai/v1"

# Statuses that mean "the same request may work shortly", the same set `claude.py` retries
# on. 408 and 409 are timeouts from opposite ends — the request and a lock — and 429 is the
# provider asking us to slow down.
_RETRYABLE_STATUS = frozenset({408, 409, 429})

#: The `error.code` Groq returns when the model emitted a tool call its own schema rejected.
#: Status 400, and the wrong reading of it — see the module docstring.
_TOOL_USE_FAILED = "tool_use_failed"

#: A short reason per status. Any status not listed falls back to a generic wording, so a
#: provider inventing a new code is reported rather than crashing the translation.
_STATUS_REASONS: dict[int, str] = {
    400: "the provider rejected the request",
    401: "the provider rejected the API key",
    403: "the API key is not permitted to use this model",
    404: "the configured model does not exist",
    408: "the provider timed out the request",
    409: "the provider reported a conflict",
    413: "the request exceeded the model's context length",
    422: "the provider could not process the request",
    429: "the provider rate-limited the request",
}

#: The `error.code` values we are willing to put in a log line. **An allowlist, not a
#: passthrough**, because the envelope around a code also carries `message` and, for
#: `tool_use_failed`, the model's whole failed generation — text derived from a customer's
#: message. Reading a key we have named is a different act from forwarding a field we have
#: not read, and only the first one is safe.
_ERROR_CODE_REASONS: dict[str, str] = {
    "invalid_api_key": "the provider rejected the API key",
    "model_not_found": "the configured model does not exist",
    "rate_limit_exceeded": "the provider rate-limited the request",
    "tool_use_failed": "the model produced a tool call that did not match the schema",
}

#: Reasoning effort for the four structured operations. See the module docstring: left at
#: Groq's default a reasoning model can spend the whole completion ceiling thinking. A model
#: that does not accept this parameter would need a code change here, in the same way that a
#: new model needs a row in `app/ai/pricing.py` — the two go together.
_REASONING_EFFORT = "low"

_client: "httpx.AsyncClient | None" = None


def _shared_client() -> "httpx.AsyncClient":
    """The process-wide client, built on first use.

    Lazy rather than module-level, for the reason `claude.py` gives: importing this module
    must not require a key to exist, or every checkout without one breaks at import. The key
    is checked here so an absent one fails loudly at the point of use — which is what makes
    `AI_API_KEY` optional in `Settings` rather than required.

    The credential goes on the client, not on each request, so the key is touched in exactly
    one place in this module and cannot be logged by a request-building path.
    """
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.AI_API_KEY:
            raise AIPermanentError("AI_API_KEY is not configured")
        _client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {settings.AI_API_KEY}"},
            # httpx has no retry mechanism to disable, so unlike the Anthropic SDK there is
            # nothing here that could quietly multiply the policy `ai_service` owns.
            timeout=settings.AI_TIMEOUT_SECONDS,
        )
    return _client


def reset_client() -> None:
    """Drop the cached client. Called on shutdown, and by tests that repoint the provider."""
    global _client
    _client = None


def _as_mapping(value: object) -> Mapping[str, Any]:
    """`value` as a mapping, or an empty one.

    Every read of a provider response goes through this, because the response is JSON from
    somebody else's service and `payload["choices"][0]["message"]` is a chain of assumptions
    about a document we did not write. A missing key becomes an empty mapping, which becomes
    the "answered without a tool call" path — a handled `AIOutputError` rather than a
    `TypeError` escaping as a 500.
    """
    return value if isinstance(value, dict) else {}


def _as_int(value: object) -> int:
    """`value` as an int, or zero. A provider that reported no usage cost nothing knowable."""
    return value if isinstance(value, int) else 0


def _error_code(response: httpx.Response) -> str | None:
    """Groq's machine-readable error code, if the body is parseable JSON that carries one.

    Returns `None` for a body that is not JSON, is not an object, or has no `error.code` —
    all of which are legal things for an error response to be, and none of which is worth
    raising a second exception about while already handling the first.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    code = _as_mapping(_as_mapping(payload).get("error")).get("code")
    return code if isinstance(code, str) else None


def _reason_for(response: httpx.Response) -> str:
    """A safe wording for a failed call: a known code first, then the status code.

    **The code is looked up, never forwarded.** `error.message` and `failed_generation` are
    readable by anyone who can read the response and are not readable here, because a reason
    string reaches a log line.
    """
    code = _error_code(response)
    if code is not None and code in _ERROR_CODE_REASONS:
        return _ERROR_CODE_REASONS[code]
    return _STATUS_REASONS.get(response.status_code, "the provider returned an error")


def _from_status(exc: httpx.HTTPStatusError) -> AIError:
    """Turn a status error into one of our three types, with a reason.

    `tool_use_failed` is checked first and is the only code that outranks its status: Groq
    signals *our answer was unusable* with a 400, and a 400 is otherwise permanent-and-not-
    our-fault. The three error types are the vocabulary `ai_service` retries on, so filing
    this under the wrong one would either retry a reproducible failure three times or report
    a schema mismatch as a provider outage.

    Everything else follows the status: 408/409/429 and 5xx are worth another attempt, and
    the rest — a bad key, a model that does not exist, a request too large — are not.
    """
    if _error_code(exc.response) == _TOOL_USE_FAILED:
        return AIOutputError(_reason_for(exc.response))

    if exc.response.status_code in _RETRYABLE_STATUS or exc.response.status_code >= 500:
        return AITransientError(_reason_for(exc.response))
    return AIPermanentError(_reason_for(exc.response))


def _translate(exc: httpx.HTTPError) -> AIError:
    """One httpx exception, one of our three types.

    `TimeoutException` is checked before `TransportError` because it is a subclass of it —
    read the other way round, every timeout would be reported as an unreachable provider,
    which is a different failure with a different fix.

    Anything unrecognised becomes permanent, which is the safe direction and the same default
    `claude.py` takes: a permanent error costs one attempt, and a transient error misclassified
    as permanent costs a retry that would have worked. The reverse mistake is the expensive one.
    """
    if isinstance(exc, httpx.TimeoutException):
        return AITransientError("the provider did not answer within the timeout")
    if isinstance(exc, httpx.TransportError):
        return AITransientError("the provider could not be reached")
    if isinstance(exc, httpx.HTTPStatusError):
        return _from_status(exc)
    return AIPermanentError(type(exc).__name__)


def _choices(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """The first choice, or an empty mapping when there is none.

    Groq supports `n > 1`; this project asks for one, so the first is the only one and a
    missing list is reported as the empty answer it is rather than indexed into.
    """
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        return _as_mapping(choices[0])
    return {}


def _usage(payload: Mapping[str, Any]) -> tuple[int, int]:
    """`(prompt_tokens, completion_tokens)` as the provider counted them.

    Read before any answer is judged, and that ordering is deliberate: a response we go on to
    reject was still billed, so the counts have to be in hand to travel with the failure.
    """
    usage = _as_mapping(payload.get("usage"))
    return _as_int(usage.get("prompt_tokens")), _as_int(usage.get("completion_tokens"))


def _tool_call(choice: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The first tool call the model made, or `None` if it answered some other way."""
    calls = _as_mapping(choice.get("message")).get("tool_calls")
    if isinstance(calls, list) and calls:
        return _as_mapping(calls[0])
    return None


class GroqProvider:
    """`AIProvider` over Groq's OpenAI-compatible API. See the module docstring."""

    name = "groq"

    async def classify_ticket(self, request: AIRequest) -> AIResult[Classification]:
        """§18 — category, subcategory, and confidence."""
        return await self._structured(
            request,
            Classification,
            tool="record_classification",
            description=(
                "Record the category, subcategory, and confidence for the support ticket."
            ),
        )

    async def analyze_sentiment(self, request: AIRequest) -> AIResult[SentimentResult]:
        """§19 — positive, neutral, or negative, with confidence."""
        return await self._structured(
            request,
            SentimentResult,
            tool="record_sentiment",
            description=(
                "Record the customer's sentiment and how confident you are in that reading."
            ),
        )

    async def summarize_conversation(self, request: AIRequest) -> AIResult[ConversationSummary]:
        """§20 — a concise summary of a conversation."""
        return await self._structured(
            request,
            ConversationSummary,
            tool="record_summary",
            description="Record a concise summary of the support conversation.",
        )

    async def generate_response(self, request: AIRequest) -> AIResult[SuggestedReply]:
        """§21 — a draft reply. Drafted here; sent by a person, never by this code."""
        return await self._structured(
            request,
            SuggestedReply,
            tool="record_reply",
            description=("Record a draft reply for the support agent to review and send."),
        )

    async def _structured[T: BaseModel](
        self,
        request: AIRequest,
        output: type[T],
        *,
        tool: str,
        description: str,
    ) -> AIResult[T]:
        """One call, and the only code path in this module that talks to the network.

        The same reduction `claude.py` makes: the four operations differ in their tool name,
        their schema, and the sentence describing the tool, and nothing else. So there is one
        implementation of the timeout, the translation, the finish-reason handling, and the
        validation — and, because the tool *names* are identical across both providers, a
        deployment can be switched between vendors without anything that reads a ledger or a
        log line learning a new vocabulary.
        """
        client = _shared_client()

        body: dict[str, Any] = {
            "model": get_settings().AI_MODEL,
            # Groq's name for it, and not `max_tokens`: the older key is deprecated across
            # OpenAI-compatible APIs and some deployments reject it outright.
            "max_completion_tokens": request.max_tokens,
            "reasoning_effort": _REASONING_EFFORT,
            "messages": [
                {"role": "system", "content": request.instruction},
                {
                    "role": "user",
                    "content": as_untrusted(request.content_label, request.content),
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool,
                        "description": description,
                        "parameters": _tool_schema(output),
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": tool}},
        }

        try:
            response = await client.post(f"{_API_ROOT}/chat/completions", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Only httpx errors are translated. Anything else escaping from here is a bug
            # rather than a provider condition, and dressing it as an AI failure would hide
            # it behind a 503 and a retry loop.
            translated = _translate(exc)
            logger.warning(
                "ai_provider_error",
                provider=self.name,
                tool=tool,
                error_type=type(exc).__name__,
                reason=translated.reason,
                retryable=isinstance(translated, AITransientError),
            )
            raise translated from exc

        try:
            payload = _as_mapping(response.json())
        except ValueError as exc:
            # A 200 whose body is not JSON. There is no usage block to read, so the failure
            # is priced from zero tokens — the honest number, since the provider told us
            # nothing about what it billed.
            raise AIOutputError("the response was not JSON") from exc

        prompt_tokens, completion_tokens = _usage(payload)
        choice = _choices(payload)
        finish_reason = choice.get("finish_reason")

        # Finish reasons first, because they explain a response that is otherwise just an
        # answer missing its tool call. Each of these tokens was billed.
        if finish_reason == "length":
            raise AIOutputError(
                "the answer was truncated at max_completion_tokens, so the tool arguments "
                "are incomplete",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        if finish_reason == "content_filter":
            # The provider's own filter stopped the answer. Retrying reproduces it — the same
            # words produce the same verdict — so this is permanent, and it is a policy
            # outcome an operator should see rather than a transient one to absorb.
            raise AIPermanentError("the provider filtered the response")

        call = _tool_call(choice)
        if call is None:
            raise AIOutputError(
                "the model answered without a tool call",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        function = _as_mapping(call.get("function"))
        if function.get("name") != tool:
            # `tool_choice` named one tool, so this means the model ignored it — worth its
            # own wording, because the fix is a prompt problem and not a schema one.
            raise AIOutputError(
                "the model called a tool that was not the one it was told to use",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        try:
            # `arguments` is a JSON *string*, not an object — the OpenAI shape, and the one
            # `validate_output` has accepted since Phase T. This line is the whole of §18's
            # migration cost to a second vendor.
            value = validate_output(output, function.get("arguments"))
        except AIOutputError as exc:
            # **The tokens are re-attached here, and only here.** `validate_output` is shared
            # with the other two providers and knows nothing about usage — it is handed a
            # payload and returns a model or raises. But an answer that was generated and then
            # rejected was billed exactly like one that was accepted, and `ai_service` prices
            # a failure from the counts riding on the exception. Without this the most
            # expensive failure there is — a full-length answer to a schema it did not match —
            # would reach the ledger as a zero-cost row: the wrong number rather than a
            # missing one. `claude.py` needs the identical block for the identical reason.
            raise AIOutputError(
                exc.reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ) from exc

        return AIResult(
            value=value,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
