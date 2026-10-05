"""OpenAI's embeddings — §22's vectors, and the fifth method §17 asked for.

ADR-008 chose Claude for generation and recorded that embeddings would come from a
*separate* vendor, because Anthropic publishes no embedding model. ADR-027 then refused to
put `generate_embedding` on `AIProvider`, because doing so would force every generation
provider to carry a method it could never serve. This module is the other half of that
decision arriving: `EmbeddingProvider` is the protocol, this is the implementation, and
neither `claude.py` nor `groq.py` changed a line to make room for it.

**A separate vendor did not cost a separate error vocabulary.** The three types in
`app/ai/errors.py` are the same three this module raises, and for the same reasons: a
transport failure or a 5xx is transient, a refused request is permanent, and an answer that
is not the shape we asked for is an output error. `ai_service._run` therefore applies §17's
retry policy, writes the ledger, and prices the call without knowing which of the five
operations it is running — which is the strongest available evidence that the boundary was
drawn in the right place. Compare this file to `groq.py`: same client discipline, same
translation, same validation entry point, and a different request body.

**One status this module reads differently, and it is not a detail.** A `429` whose
`error.code` is `insufficient_quota` means the account is out of credit. Groq's 429 means
"slow down" and retrying is exactly right; this one cannot succeed however long the caller
waits, and treating it as transient would spend the whole attempt budget — three sleeps and
three refused requests — to arrive at the same failure. So the code is checked before the
status and mapped to permanent, which is one query in an allowlist buying a faster and more
accurate failure.

**The response's `index` field is what the vectors are ordered by, not the order they
arrived in.** The API documents a per-entry index and the count is checked against the number
of texts sent, because a caller is about to write those vectors against the passages it split
a document into: one vector attached to the wrong passage is an answer that reads fluently and
cites the wrong policy, and nothing downstream could detect it. Sorting by the index the
provider gave and refusing a short list is the whole of the defence.

**No fence, and its absence is deliberate.** The generation providers wrap untrusted text
because a document can contain instructions and a language model reads instructions. An
embedding model does not — the `input` field is data to be mapped into a vector, and there is
no instruction in this request for a document to hijack. Applying `as_untrusted` here would
put fence markers *into* the embedded text, which would change the vector for no gain.

**`httpx`, not the OpenAI SDK**, for the reason `groq.py` gives: the endpoint is one POST, the
SDK would be a second dependency with its own retry machinery to disable, and this project
already owns a client discipline — a per-loop cached client with no retries, because retries
belong to `ai_service`.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx
import structlog

from app.ai.errors import AIError, AIOutputError, AIPermanentError, AITransientError
from app.ai.provider import AIResult, validate_output
from app.core.config import get_settings
from app.core.event_loop import current_loop
from app.schemas.knowledge import Embedding

logger = structlog.get_logger(__name__)

_API_ROOT = "https://api.openai.com/v1"

# Statuses that mean "the same request may work shortly", the same set `claude.py` and
# `groq.py` retry on.
_RETRYABLE_STATUS = frozenset({408, 409, 429})

#: The `error.code` that outranks its status. A 429 normally means "slow down"; this one means
#: the account has no credit left, which no amount of waiting changes. See the module docstring.
_INSUFFICIENT_QUOTA = "insufficient_quota"

#: A short reason per status, with a generic fallback so a code we have not seen is reported
#: rather than crashing the translation.
_STATUS_REASONS: dict[int, str] = {
    400: "the provider rejected the request",
    401: "the provider rejected the API key",
    403: "the API key is not permitted to use this model",
    404: "the configured embedding model does not exist",
    408: "the provider timed out the request",
    409: "the provider reported a conflict",
    413: "the request exceeded the model's token limit",
    422: "the provider could not process the request",
    429: "the provider rate-limited the request",
}

#: The `error.code` values we are willing to name in a log line. An allowlist rather than a
#: passthrough, the discipline `groq.py` established: the envelope around a code also carries
#: `message`, and a reason string reaches a log.
_ERROR_CODE_REASONS: dict[str, str] = {
    "invalid_api_key": "the provider rejected the API key",
    "insufficient_quota": "the account has no remaining quota",
    "model_not_found": "the configured embedding model does not exist",
    "rate_limit_exceeded": "the provider rate-limited the request",
    "context_length_exceeded": "a passage exceeded the model's token limit",
}

_client: "httpx.AsyncClient | None" = None

#: The loop `_client` was built on. Same trap and same handling as `groq.py`: an `httpx` pool
#: belongs to the loop that opened it, and `event_loop.run` builds and closes one per Celery
#: task, so a client cached across two tasks points at a loop that no longer exists. `None` is
#: the case where a test swapped `_client` for a double, which is not this module's to discard.
_client_loop: "asyncio.AbstractEventLoop | None" = None


def _shared_client() -> "httpx.AsyncClient":
    """The client for the running loop, built on first use on that loop.

    Lazy so that importing this module does not require a key to exist — the reason
    `EMBEDDING_API_KEY` is optional in `Settings` and a checkout with no key still starts.
    The credential is set on the client rather than per request, so it is touched in exactly
    one place and cannot be logged by a request-building path.
    """
    global _client, _client_loop
    running = current_loop()
    if (
        running is not None
        and _client is not None
        and _client_loop is not None
        and _client_loop is not running
    ):
        _client = None
    if _client is None:
        settings = get_settings()
        if not settings.EMBEDDING_API_KEY:
            raise AIPermanentError("EMBEDDING_API_KEY is not configured")
        _client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {settings.EMBEDDING_API_KEY}"},
            timeout=settings.AI_TIMEOUT_SECONDS,
        )
        _client_loop = running
    return _client


def reset_client() -> None:
    """Drop the cached client. Called on shutdown, and by tests that repoint the provider."""
    global _client, _client_loop
    _client = None
    _client_loop = None


def _as_mapping(value: object) -> Mapping[str, Any]:
    """`value` as a mapping, or an empty one — the guard `groq.py` applies to every read.

    The response is JSON from somebody else's service, and `payload["data"][0]["embedding"]`
    is a chain of assumptions about a document we did not write. A missing key becomes an
    empty mapping, which becomes a handled `AIOutputError` rather than a `TypeError` escaping
    as a 500.
    """
    return value if isinstance(value, dict) else {}


def _as_int(value: object) -> int:
    """`value` as an int, or zero. A provider that reported no usage spent nothing knowable."""
    return value if isinstance(value, int) else 0


def _error_code(response: httpx.Response) -> str | None:
    """OpenAI's machine-readable error code, if the body is parseable JSON carrying one."""
    try:
        payload = response.json()
    except ValueError:
        return None
    code = _as_mapping(_as_mapping(payload).get("error")).get("code")
    return code if isinstance(code, str) else None


def _reason_for(response: httpx.Response) -> str:
    """A safe wording for a failed call: a known code first, then the status code.

    The code is looked up, never forwarded, for the reason `groq.py` gives: the envelope
    around a code carries a `message` that can quote the request.
    """
    code = _error_code(response)
    if code is not None and code in _ERROR_CODE_REASONS:
        return _ERROR_CODE_REASONS[code]
    return _STATUS_REASONS.get(response.status_code, "the provider returned an error")


def _from_status(exc: httpx.HTTPStatusError) -> AIError:
    """Turn a status error into one of our three types, with a reason.

    `insufficient_quota` is checked first and is the only code that outranks its status — see
    the module docstring. Everything else follows the status: 408/409/429 and 5xx are worth
    another attempt, and the rest are not.
    """
    if _error_code(exc.response) == _INSUFFICIENT_QUOTA:
        return AIPermanentError(_reason_for(exc.response))

    if exc.response.status_code in _RETRYABLE_STATUS or exc.response.status_code >= 500:
        return AITransientError(_reason_for(exc.response))
    return AIPermanentError(_reason_for(exc.response))


def _translate(exc: httpx.HTTPError) -> AIError:
    """One httpx exception, one of our three types.

    `TimeoutException` before `TransportError`, because it is a subclass — read the other way
    round every timeout would be reported as an unreachable provider, which is a different
    failure with a different fix. Anything unrecognised becomes permanent, which is the safe
    direction: a permanent error costs one attempt, and a transient error misread as permanent
    costs a retry that would have worked.
    """
    if isinstance(exc, httpx.TimeoutException):
        return AITransientError("the provider did not answer within the timeout")
    if isinstance(exc, httpx.TransportError):
        return AITransientError("the provider could not be reached")
    if isinstance(exc, httpx.HTTPStatusError):
        return _from_status(exc)
    return AIPermanentError(type(exc).__name__)


def _index_of(entry: object, fallback: int) -> int:
    """The position `entry` claims for itself, or the position it arrived in."""
    index = _as_mapping(entry).get("index")
    return index if isinstance(index, int) else fallback


def _vectors(payload: Mapping[str, Any]) -> list[object]:
    """The embeddings, ordered by the index the provider gave each one.

    Deliberately untyped inside: each entry's shape is checked by `Embedding` in
    `app/schemas/knowledge.py`, which is the point of routing a payload through
    `validate_output` rather than inspecting it here. This function's only job is the order,
    and the order is the provider's own claim about which vector answers which input.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    ordered = sorted(enumerate(data), key=lambda pair: _index_of(pair[1], pair[0]))
    return [_as_mapping(entry).get("embedding") for _, entry in ordered]


class OpenAIEmbeddingProvider:
    """`EmbeddingProvider` over OpenAI's embeddings endpoint. See the module docstring."""

    name = "openai"

    async def generate_embedding(self, texts: list[str]) -> AIResult[Embedding]:
        """One batched call, then §18's validation, then the count check.

        The tokens are read before the answer is judged, for the reason `groq.py` gives: a
        response we go on to reject was still billed, and the counts have to be in hand to
        travel with the failure so `ai_service` can price it.
        """
        client = _shared_client()

        try:
            response = await client.post(
                f"{_API_ROOT}/embeddings",
                json={"model": get_settings().EMBEDDING_MODEL, "input": texts},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Only httpx errors are translated. Anything else escaping from here is a bug
            # rather than a provider condition, and dressing it as an AI failure would hide it
            # behind a 503 and a retry loop.
            translated = _translate(exc)
            logger.warning(
                "ai_provider_error",
                provider=self.name,
                operation="embed",
                texts=len(texts),
                error_type=type(exc).__name__,
                reason=translated.reason,
                retryable=isinstance(translated, AITransientError),
            )
            raise translated from exc

        try:
            payload = _as_mapping(response.json())
        except ValueError as exc:
            raise AIOutputError("the response was not JSON") from exc

        # `prompt_tokens` is the whole of what an embedding call spends: the vendor bills the
        # text it read and there is nothing generated to bill. `total_tokens` is not read —
        # it is the same number, and reading two keys for one fact is how they come apart.
        prompt_tokens = _as_int(_as_mapping(payload.get("usage")).get("prompt_tokens"))

        try:
            value = validate_output(Embedding, {"vectors": _vectors(payload)})
        except AIOutputError as exc:
            # The tokens ride on the exception, the `groq.py` block for the `groq.py` reason: a
            # response that was generated and then rejected was billed exactly like one that
            # was accepted, and without this the ledger would record a zero-cost row.
            raise AIOutputError(exc.reason, prompt_tokens=prompt_tokens) from exc

        if len(value.vectors) != len(texts):
            # Not a partial success. The caller is about to write these vectors against the
            # passages it sent, and a misalignment would attach one passage's meaning to
            # another passage's text — silently, and permanently.
            raise AIOutputError(
                f"the provider returned {len(value.vectors)} vectors for {len(texts)} texts",
                prompt_tokens=prompt_tokens,
            )

        return AIResult(value=value, prompt_tokens=prompt_tokens, completion_tokens=0)
