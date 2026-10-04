"""The vendor boundary — §17's `AIProvider`, and the one place model output is parsed.

Spec §17: *"Create AI provider abstraction"*, and architecture §6 names the interface it
means: `classify_ticket`, `analyze_sentiment`, `summarize_conversation`,
`generate_response`. Nothing above this module imports a vendor SDK, and nothing below it
knows what an `AIOperation` is. Swapping providers means writing one more class here.

**`generate_embedding` is absent, and that is ADR-008 being honoured rather than
forgotten.** §17 lists five methods; ADR-008 records that embeddings come from a
*separate* vendor behind the same interface and defers that choice to Phase X. Declaring
the method now would force every provider to carry one it cannot serve — a stub with no
caller, which is what this codebase refuses everywhere else: settings wait for their
consumer, the `ai` queue waits for a producer. Phase X adds the method with the provider
that can answer it.

**Validation lives in `validate_output`, not in each provider.** §18 wants one guarantee —
*"Never assume LLM output is automatically valid"* — and a guarantee implemented once per
vendor is a guarantee with as many implementations as there are vendors. Each provider's
job is reduced to getting a payload out of its own SDK; turning that payload into a
Pydantic model, or refusing to, happens here. So the malformed-output test needs no
vendor at all, and a new provider inherits the §18 behaviour by using this function.

**`_tool_schema` is here for the same reason, one layer up.** Both vendors ask the model to
answer by calling a tool constrained by a JSON Schema; they differ in how the schema is
enveloped and in nothing else. The envelope belongs to the provider. The schema does not.

**Providers do not retry, do not sleep, and do not measure.** Timeout, backoff, attempt
counts, cost, and the ledger belong to `app/services/ai_service.py`, which is the only
caller. A provider that also retried would multiply the two policies, and the observed
attempt count would stop meaning anything. `AsyncAnthropic` is constructed with
`max_retries=0` for exactly this reason (ADR-027).
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import BaseModel, ValidationError

from app.ai.errors import AIOutputError
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)

#: How many distinct field failures are named in an `AIOutputError`. A model that
#: ignored the schema can fail on every field at once, and the message is logged —
#: an uncapped one would put a multi-kilobyte string in the log for no extra
#: information, because the first few failures already say what was misunderstood.
_MAX_REPORTED_FAILURES = 5


@dataclass(frozen=True)
class AIRequest:
    """One call: what to do, what to do it to, and how much room to answer.

    `instruction` becomes the provider's system prompt and `content` becomes the user
    message — the split every vendor makes, stated once here rather than in each
    implementation.

    **`content` is customer-written text and `content_label` is not.** The label is the
    caller's own words ("the customer's message"), used to introduce the block, and a
    caller that passed a ticket's subject as a label would be moving untrusted text
    outside the fence — see `app/ai/prompts.py`, which the provider applies so that
    fencing is not something a caller can forget.

    `max_tokens` is resolved by `ai_service` from `Settings`, so it has one home rather
    than a default per provider.
    """

    instruction: str
    content: str
    content_label: str
    max_tokens: int


@dataclass(frozen=True)
class AIResult[T]:
    """What a provider returned: the validated model, and what it cost in tokens.

    The token counts are the vendor's own, passed through untouched. `ai_service`
    prices them and writes the ledger row; a provider that also computed `cost_usd`
    would have to know the rate table, which is application knowledge and not vendor
    knowledge.

    **No latency field.** `ai_service` measures elapsed time around the call, which is
    the only place that sees every attempt — including the ones that raise and so have
    no `AIResult` to read a number from.
    """

    value: T
    prompt_tokens: int
    completion_tokens: int


def _failure_summary(exc: ValidationError) -> str:
    """Name the rules a payload broke, without repeating what it said.

    `ValidationError.errors()` carries the offending `input` beside each failure, and for
    this schema that input is model-authored text derived from a customer's message. The
    type and location are the whole of the useful information — "confidence:
    less_than_equal" tells an operator that the model misread the range, and quoting the
    `1.4` adds nothing a log should hold. So only `type` and `loc` come through.

    This is the same reasoning `app/ai/errors.py` gives for not carrying the provider's
    own message, applied one layer in.
    """
    failures = [
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
        for error in exc.errors()[:_MAX_REPORTED_FAILURES]
    ]
    return "; ".join(failures)


def validate_output[T: BaseModel](output: type[T], payload: object) -> T:
    """Turn a provider's raw payload into `output`, or raise `AIOutputError`.

    The payload may be a JSON string, an already-decoded mapping, or an instance of
    `output` itself. The last case is a short circuit and not a loophole: a value that is
    already an `output` cannot fail to be one. It exists so a test double can script a
    real result without serialising it first.

    **Every rejection produces the same error type**, whether the payload was prose
    instead of JSON, JSON that was not an object, an object missing a field, a
    confidence of `1.4`, or a sentiment the model invented. They are one condition —
    the answer could not be trusted — and §18 asks for one handling of it. The reason
    string distinguishes them for the log.

    The returned model is the only thing a caller ever receives. There is no path from a
    provider response to application code that skips this function.
    """
    if isinstance(payload, output):
        return payload

    decoded: object = payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except ValueError as exc:
            # ValueError rather than JSONDecodeError alone: the pure-Python and C
            # decoders have raised different subclasses across versions, and this
            # module should not care which one is compiled in.
            raise AIOutputError("the response was not JSON") from exc

    if not isinstance(decoded, dict):
        raise AIOutputError(f"the response was {type(decoded).__name__}, not an object")

    try:
        return output.model_validate(decoded)
    except ValidationError as exc:
        raise AIOutputError(
            f"the response did not match {output.__name__}: {_failure_summary(exc)}"
        ) from exc


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    """Replace every `$ref` in `node` with the definition it names, recursively.

    Pydantic emits a non-primitive field as `{"$ref": "#/$defs/Sentiment"}` beside a
    `$defs` block, and the tool schema actually sent to the provider should contain
    neither: the schema is read by a model as much as by a validator, and an indirection
    it has to chase is one more thing to get wrong. Two of the four schemas carry such a
    field — `SentimentResult.sentiment` and, since Phase U, `Classification.priority` —
    each at one level of nesting.

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
    """The JSON Schema a tool call must satisfy, with `$ref`s resolved.

    **This lives here rather than in a provider, and the second provider is what proved
    it.** It was written for Anthropic in Phase T; Groq needs the same document in its
    own envelope, because both vendors constrain a tool call with JSON Schema. The two
    envelopes differ — Anthropic takes `input_schema` beside `tool_choice: {"type":
    "tool"}` and Groq takes it under `function.parameters` beside `tool_choice: {"type":
    "function"}` — but the *schema* is the same object, and the shape of a schema is not
    a vendor's business. A copy of this in `groq.py` would be two implementations of §18's
    guarantee, which is the thing `validate_output` exists to prevent one layer up.
    """
    schema: dict[str, Any] = output.model_json_schema()
    defs: dict[str, Any] = schema.pop("$defs", {})
    if not defs:
        return schema
    # `_inline` is untyped inside because it walks an arbitrary JSON document. The cast
    # states what is true of the entry point: given a schema it returns a schema.
    return cast("dict[str, Any]", _inline(schema, defs))


class AIProvider(Protocol):
    """§17's interface. One method per operation the AI layer can perform.

    Every method raises `AITransientError`, `AIPermanentError`, or `AIOutputError` and
    nothing else — an implementation that let a vendor exception escape would make the
    retry policy in `ai_service` depend on which provider was configured.

    `name` is written into the ledger's `provider` column, so a cost report can say which
    vendor produced the spend even after the deployment has moved to another one.
    """

    name: str

    async def classify_ticket(self, request: AIRequest) -> AIResult[Classification]:
        """§18 — category, subcategory, and confidence for a ticket."""
        ...

    async def analyze_sentiment(self, request: AIRequest) -> AIResult[SentimentResult]:
        """§19 — positive, neutral, or negative, with confidence."""
        ...

    async def summarize_conversation(self, request: AIRequest) -> AIResult[ConversationSummary]:
        """§20 — a concise summary of a conversation."""
        ...

    async def generate_response(self, request: AIRequest) -> AIResult[SuggestedReply]:
        """§21 — a draft reply for an agent to review and send themselves."""
        ...
