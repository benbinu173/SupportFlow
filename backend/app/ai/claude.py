"""Claude — the only module in the codebase that knows the SDK exists.

Architecture §6: *"Application code depends on an `AIProvider` interface, never on a
vendor SDK."* This is where that stops being a diagram: `anthropic` is imported here and
nowhere else, and everything that escapes is one of the three types in `app/ai/errors.py`.

**Structured output is requested as a tool call.** ADR-008 chose it, and the reason holds:
a tool's `input_schema` is a JSON Schema the model is told to satisfy, so the constraint
is stated to the model rather than hoped for in prose. `tool_choice` names the tool, which
means the model has no way to answer with a paragraph instead — the one case §18 cares
most about. The answer still goes through `validate_output` afterwards. The API constrains
the shape; we check it, because the schema reaching the model is not the same thing as our
Pydantic model accepting what comes back, and §18 does not say "assume the provider
validated it".

**The SDK's own retries are off** (`max_retries=0`), so the attempt count, the backoff, and
the timeout are one policy in one place instead of two policies multiplied together.
`APITimeoutError` is translated like any other failure and `ai_service` decides what to do.

**Failure classification mirrors the SDK's, deliberately.** `_translate` walks the
`__cause__` chain and applies the same rules `BaseClient._should_retry` applies — retry
408, 409, 429, and 5xx, retry connection and timeout errors, obey an explicit
`x-should-retry` header — because the alternative is a policy that disagrees with the
vendor's own guidance about the vendor's own service. The difference is only *who* acts on
it: the SDK would have retried internally, and instead the answer is translated and
`ai_service` retries.

**Prompts are not logged, and neither is anything the model said.** A request carries the
API key in a header and a customer's words in the body. So a log line here gets
`error_type=type(exc).__name__` — the pattern `app/workers/email_tasks.py` set — plus our
own reason string, which is written in this module and quotes nothing.
"""

from typing import Any, cast

import structlog
from anthropic import (
    AnthropicError,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    RetryableError,
)
from anthropic.types import ToolUseBlock
from pydantic import BaseModel

from app.ai.errors import AIError, AIOutputError, AIPermanentError, AITransientError
from app.ai.prompts import as_untrusted
from app.ai.provider import AIRequest, AIResult, validate_output
from app.core.config import get_settings
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)

logger = structlog.get_logger(__name__)

# Statuses that mean "the same request may work shortly". 408 and 409 are timeouts from
# opposite ends — the request and a lock — and 429 is the provider asking us to slow down.
_RETRYABLE_STATUS = frozenset({408, 409, 429})

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

_client: "AsyncAnthropic | None" = None


def _shared_client() -> "AsyncAnthropic":
    """The process-wide client, built on first use.

    One client, not one per call: it owns a connection pool, and the TLS handshakes a
    per-call client would repeat are a measurable part of a call whose whole cost is
    dominated by the model thinking. Lazy rather than module-level so importing this
    module never requires a key to exist — the test suite imports it, and a required key
    at import time would break every checkout that has none.

    **The key is checked here, so the failure is loud and local.** An absent key raises
    `AIPermanentError` at the point of use rather than at import, which is why
    `AI_API_KEY` is optional in `Settings`: absent is a legal configuration for a
    deployment that has not enabled AI, and this is the line that says so.
    """
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.AI_API_KEY:
            raise AIPermanentError("AI_API_KEY is not configured")
        _client = AsyncAnthropic(
            api_key=settings.AI_API_KEY,
            # See the module docstring: the retry policy is `ai_service`'s.
            max_retries=0,
            timeout=settings.AI_TIMEOUT_SECONDS,
        )
    return _client


def reset_client() -> None:
    """Drop the cached client. Called on shutdown, and by tests that repoint the provider."""
    global _client
    _client = None


def _reason_for_status(exc: APIStatusError) -> str:
    return _STATUS_REASONS.get(exc.status_code, "the provider returned an error")


def _from_status(exc: APIStatusError) -> AIError:
    """Turn a status error into transient or permanent, with a reason.

    The `x-should-retry` check comes first because the SDK puts it first, and it is the
    one input that can overrule the status code — a 500 the provider has already told us
    not to retry is a poisoned request, and retrying it `AI_MAX_ATTEMPTS` times is exactly
    the repeated-call waste §53 names. It is a non-standard header, which is why it is
    commented rather than assumed.
    """
    header = exc.response.headers.get("x-should-retry")
    if header == "false":
        return AIPermanentError(_reason_for_status(exc))
    if header == "true":
        return AITransientError(_reason_for_status(exc))

    if exc.status_code in _RETRYABLE_STATUS or exc.status_code >= 500:
        return AITransientError(_reason_for_status(exc))
    return AIPermanentError(_reason_for_status(exc))


def _translate(exc: AnthropicError) -> AIError:
    """One SDK exception, one of our three types.

    Walks `__cause__` for the same reason the SDK does: a retryable failure wrapped with
    `raise ... from` is still a retryable failure, and the outermost exception is often
    the least informative one in the chain.

    Anything unrecognised becomes permanent. That is the SDK's own default — it returns
    "do not retry" for an exception it cannot classify — and it is the safe direction: a
    permanent error costs one attempt, and a transient error misclassified as permanent
    costs a retry that would have worked. The reverse mistake is the expensive one.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RetryableError):
            return AITransientError("the provider reported a retryable failure")
        if isinstance(current, APITimeoutError):
            return AITransientError("the provider did not answer within the timeout")
        if isinstance(current, APIConnectionError):
            return AITransientError("the provider could not be reached")
        if isinstance(current, APIStatusError):
            return _from_status(current)
        current = current.__cause__
    return AIPermanentError(type(exc).__name__)


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    """Replace every `$ref` in `node` with the definition it names, recursively.

    Pydantic emits a non-primitive field as `{"$ref": "#/$defs/Sentiment"}` beside a
    `$defs` block, and the tool schema actually sent to the provider should contain
    neither: the schema is read by a model as much as by a validator, and an indirection
    it has to chase is one more thing to get wrong. Our `Sentiment` field is the only
    `$ref` the four schemas have, at one level of nesting.

    A self-referential schema would exhaust the recursion limit rather than loop
    forever — none of the four is, and `tests/unit/test_ai_structured_output.py` walks
    every schema that reaches the provider.
    """
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        return _inline(defs[ref.rsplit("/", 1)[-1]], defs)
    return {key: _inline(value, defs) for key, value in node.items()}


def _tool_schema(output: type[BaseModel]) -> dict[str, Any]:
    """The JSON Schema a tool call must satisfy, with `$ref`s resolved."""
    schema: dict[str, Any] = output.model_json_schema()
    defs: dict[str, Any] = schema.pop("$defs", {})
    if not defs:
        return schema
    # `_inline` is untyped inside because it walks an arbitrary JSON document. The cast
    # states what is true of the entry point: given a schema it returns a schema.
    return cast("dict[str, Any]", _inline(schema, defs))


class ClaudeProvider:
    """`AIProvider` over Anthropic's API. See the module docstring for the mechanism."""

    name = "anthropic"

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

        Every operation reduces to this, which is the point: the four §17 methods differ
        in their tool name, their schema, and the sentence describing the tool — nothing
        else — so there is exactly one implementation of the timeout, the translation, the
        stop-reason handling, and the validation.
        """
        client = _shared_client()

        try:
            response = await client.messages.create(
                model=get_settings().AI_MODEL,
                max_tokens=request.max_tokens,
                system=request.instruction,
                messages=[
                    {
                        "role": "user",
                        "content": as_untrusted(request.content_label, request.content),
                    }
                ],
                tools=[
                    {
                        "name": tool,
                        "description": description,
                        "input_schema": _tool_schema(output),
                    }
                ],
                tool_choice={"type": "tool", "name": tool},
            )
        except AnthropicError as exc:
            # Only SDK errors are translated. Anything else escaping from here is a bug
            # rather than a provider condition, and dressing it as an AI failure would
            # hide it behind a 503 and a retry loop.
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

        prompt_tokens = response.usage.input_tokens
        completion_tokens = response.usage.output_tokens

        # Stop reasons first, because they explain a response that is otherwise just an
        # answer missing its tool call. Each of these tokens was billed.
        if response.stop_reason == "max_tokens":
            raise AIOutputError(
                "the answer was truncated at max_tokens, so the tool arguments are incomplete",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        if response.stop_reason == "refusal":
            # A refusal is the model declining, not a malformed answer. Retrying
            # reproduces it — the same words produce the same refusal — so this is
            # permanent, and it is a policy outcome an operator should see.
            raise AIPermanentError("the model declined the request")
        if response.stop_reason == "model_context_window_exceeded":
            raise AIPermanentError("the request exceeded the model's context window")

        block = next((item for item in response.content if isinstance(item, ToolUseBlock)), None)
        if block is None:
            raise AIOutputError(
                "the model answered without a tool call",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        if block.name != tool:
            # `tool_choice` named one tool, so this means the model ignored it — worth
            # its own wording, because the fix is a prompt problem and not a schema one.
            raise AIOutputError(
                "the model called a tool that was not the one it was told to use",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        try:
            value = validate_output(output, block.input)
        except AIOutputError as exc:
            # **The tokens are re-attached here, and only here.** `validate_output` is
            # shared with `fake.py` and knows nothing about usage — it is handed a payload
            # and returns a model or raises. But an answer that was generated and then
            # rejected was billed exactly like one that was accepted, and `ai_service`
            # prices a failure from the counts riding on the exception. Without this the
            # most expensive failure there is — a full-length answer to a schema it did not
            # match — would reach the ledger as a zero-cost row, which is the wrong number
            # rather than a missing one.
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
