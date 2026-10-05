"""OpenAI's embeddings — the translation, and the one status it reads differently.

ADR-032's claim is that a second *vendor* for vectors costs one module and no new machinery:
the three error types, the retry loop, the ledger and the pricing are all `app/ai/`'s and
`app/services/ai_service.py`'s, and this file is where "no new machinery" is checked rather
than asserted. The shape of it is the evidence — there is no OpenAI SDK to fake, because the
"SDK" is one HTTP POST, and `httpx.MockTransport` answers with bytes we choose.

**What is not retested here.** `validate_output`, the retry policy, the ledger, and
`Embedding`'s width validator are covered by `test_ai_structured_output.py`, `test_ai_retry.py`
and `test_ai_pricing.py` against the fake and directly. What is tested here is the part that is
*only* this provider, and there are three of them:

* **Which status becomes which of the three error types**, and in particular the one case where
  the status is not enough — a `429` carrying `insufficient_quota` is permanent, not transient.
  That is the single behaviour in this module a reader could get wrong by pattern-matching
  `groq.py`, so it gets three tests rather than one.
* **The ordering by the provider's own `index`**, because the caller is about to attach these
  vectors to the passages it split a document into — a misalignment attaches one passage's
  meaning to another passage's text, silently and permanently.
* **That a rejected response still reports its tokens**, because it was billed exactly like an
  accepted one and `AIUsage`'s docstring is that such a call is recorded rather than dropped.

**The reason string is never the provider's text.** The body of a failed call carries a
`message` that can quote the request — and the request carries the key in a header and the
customer's documents in the body — so a test asserts the reason is one of ours and the
provider's message is not in it.
"""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.ai import openai_embedding
from app.ai.errors import AIOutputError, AIPermanentError, AITransientError
from app.ai.pricing import rate_for
from app.models.knowledge_chunk import EMBEDDING_DIMENSIONS
from app.schemas.knowledge import Embedding

pytestmark = pytest.mark.unit

MODEL = "text-embedding-3-small"

#: The credential, so that its *absence* from a request body, a reason string, or a log line is
#: observable rather than vacuous. `test_ai_log_hygiene.py` sets the same example.
API_KEY = "sk-this-key-must-never-be-logged-9f3a"

TEXTS = ["Refunds take 5 working days.", "Shipping is free over 50 euros."]


def _vector(fill: float = 0.1, width: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """A vector of the column's width, distinguishable per text by `fill`."""
    return [fill] * width


def _response(
    vectors: list[list[float]],
    *,
    indices: list[int] | None = None,
    prompt_tokens: int = 12,
) -> dict[str, Any]:
    """A response body in the shape OpenAI sends — trimmed to the fields this module reads.

    `indices` exists to express the one thing a real provider does that a naive reader would
    not expect: the entries arrive carrying their own position, and the order they arrive in is
    not necessarily it.
    """
    positions = indices if indices is not None else list(range(len(vectors)))
    return {
        "object": "list",
        "model": MODEL,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in zip(positions, vectors, strict=True)
        ],
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }


class Wire:
    """A stand-in socket: what the test wants OpenAI to answer, and what it was asked.

    One scripted outcome per test rather than a queue, `test_ai_groq.py`'s shape and for its
    reason: nothing here retries — the retry loop is `ai_service`'s — and a provider that
    retried would be the defect `test_ai_retry.py` exists to catch.
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
    """Point `OpenAIEmbeddingProvider` at a `MockTransport`, with a key in play.

    `_client` is replaced rather than `httpx.AsyncClient`, `test_ai_groq.py`'s choice: the
    module's own lazy-singleton and its credential check are bypassed so the request under test
    is the one `generate_embedding` actually builds. That the client *would* carry the key is
    asserted separately, against the module's own `_shared_client`.

    **`_client_loop` is replaced too, and leaving it alone was a bug in this file's first
    version.** The module discards a cached client whose loop has since closed, and a test that
    builds a *real* client records the loop it was built on — so the next test's double would be
    discarded as stale and replaced by a genuine `httpx.AsyncClient` making a genuine request to
    OpenAI. Both globals are the fixture's to own; the tests that failed were the ones after the
    real-client test, which is what made the leak legible.
    """
    socket = Wire()
    client = httpx.AsyncClient(transport=httpx.MockTransport(socket.handle))
    monkeypatch.setattr(openai_embedding, "_client", client)
    monkeypatch.setattr(openai_embedding, "_client_loop", None)
    monkeypatch.setattr(
        openai_embedding,
        "get_settings",
        lambda: SimpleNamespace(
            EMBEDDING_API_KEY=API_KEY, EMBEDDING_MODEL=MODEL, AI_TIMEOUT_SECONDS=30.0
        ),
    )
    try:
        yield socket
    finally:
        await client.aclose()


def _provider() -> openai_embedding.OpenAIEmbeddingProvider:
    return openai_embedding.OpenAIEmbeddingProvider()


# ---------------------------------------------------------------------------
# A valid answer, and what was sent to get it
# ---------------------------------------------------------------------------


async def test_a_batch_of_texts_becomes_validated_vectors(wire: Wire) -> None:
    """The success path, end to end, with the numbers the ledger will record.

    `completion_tokens` is zero because there is nothing generated to bill — the same input
    produces the same vector — and `AIResult` carries it as a fact rather than as a missing
    field its callers have to special-case.
    """
    wire.answers(_response([_vector(0.1), _vector(0.2)], prompt_tokens=17))

    result = await _provider().generate_embedding(TEXTS)

    assert isinstance(result.value, Embedding)
    assert result.value.vectors == [_vector(0.1), _vector(0.2)]
    assert (result.prompt_tokens, result.completion_tokens) == (17, 0)


async def test_the_request_names_the_configured_model_and_carries_no_credential(
    wire: Wire,
) -> None:
    """One POST to the embeddings endpoint, with the model and the texts and nothing else.

    The key travels in the client's `Authorization` header rather than in the body, so
    `json.dumps` of what was sent is a place the credential *cannot* be — which is asserted
    here because "the key is set once, in one place" is the property the log-hygiene tests
    depend on and this is where it is cheap to check.
    """
    wire.answers(_response([_vector(), _vector()]))

    await _provider().generate_embedding(TEXTS)

    assert str(wire.requests[0].url) == "https://api.openai.com/v1/embeddings"
    assert wire.bodies[0] == {"model": MODEL, "input": TEXTS}
    assert API_KEY not in json.dumps(wire.bodies[0])


async def test_the_credential_is_on_the_client_the_module_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_shared_client` is the one place the key is read, and it puts it in the header.

    Built here rather than mocked, because the alternative is to assert that a stub has the
    header — and the question is whether the *module* does. No request is made, so nothing
    leaves the machine; the client is closed in the test.
    """
    monkeypatch.setattr(openai_embedding, "_client", None)
    monkeypatch.setattr(openai_embedding, "_client_loop", None)
    monkeypatch.setattr(
        openai_embedding,
        "get_settings",
        lambda: SimpleNamespace(EMBEDDING_API_KEY=API_KEY, AI_TIMEOUT_SECONDS=30.0),
    )

    client = openai_embedding._shared_client()

    try:
        assert client.headers["authorization"] == f"Bearer {API_KEY}"
    finally:
        await client.aclose()


async def test_vectors_are_ordered_by_the_index_the_provider_gave_them(wire: Wire) -> None:
    """**The misalignment defence, and the reason the entries are not read in arrival order.**

    The response lists the second text's vector first. Reading the list as it arrives would
    attach "Shipping is free" to the refund passage — and nothing downstream could tell, because
    a vector is a vector and the answer that followed would read fluently while citing the wrong
    policy. Sorting by the provider's own `index` is the whole of the fix.
    """
    wire.answers(_response([_vector(0.2), _vector(0.1)], indices=[1, 0]))

    result = await _provider().generate_embedding(TEXTS)

    assert result.value.vectors == [_vector(0.1), _vector(0.2)]


async def test_an_entry_with_no_index_keeps_the_position_it_arrived_in(wire: Wire) -> None:
    """The fallback, and it is not a decoration: a provider that stopped sending `index` would
    otherwise have every vector sorted by a default of zero, which is a permutation rather than
    a no-op."""
    payload = _response([_vector(0.1), _vector(0.2)])
    del payload["data"][0]["index"]
    wire.answers(payload)

    result = await _provider().generate_embedding(TEXTS)

    assert result.value.vectors == [_vector(0.1), _vector(0.2)]


async def test_the_prompt_token_count_is_read_and_total_tokens_is_not(wire: Wire) -> None:
    """One number for one fact. `total_tokens` equals it today, and reading both is how they
    come apart — so a body where they disagree is the test, and the prompt count wins."""
    payload = _response([_vector(), _vector()], prompt_tokens=7)
    payload["usage"]["total_tokens"] = 999
    wire.answers(payload)

    result = await _provider().generate_embedding(TEXTS)

    assert result.prompt_tokens == 7


async def test_the_reported_vendor_is_the_one_the_rate_table_names(wire: Wire) -> None:
    """`name` is the ledger's source of truth for the vendor, and the rate table has a column
    that has to agree with it.

    Configuration refuses `EMBEDDING_PROVIDER` and `EMBEDDING_MODEL` from different vendors by
    looking at that column, so a `name` that drifted from it — `openai` here, `groq` there —
    would let a mismatched pair through the startup check and fail as a 401 at the first call.
    """
    wire.answers(_response([_vector(), _vector()]))

    await _provider().generate_embedding(TEXTS)

    assert _provider().name == "openai"
    assert rate_for(MODEL).provider == _provider().name


# ---------------------------------------------------------------------------
# The status, and the one code that outranks it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected", "reason"),
    [
        (408, AITransientError, "the provider timed out the request"),
        (409, AITransientError, "the provider reported a conflict"),
        (429, AITransientError, "the provider rate-limited the request"),
        (500, AITransientError, "the provider returned an error"),
        (503, AITransientError, "the provider returned an error"),
        (400, AIPermanentError, "the provider rejected the request"),
        (401, AIPermanentError, "the provider rejected the API key"),
        (403, AIPermanentError, "the API key is not permitted to use this model"),
        (404, AIPermanentError, "the configured embedding model does not exist"),
        (413, AIPermanentError, "the request exceeded the model's token limit"),
        (422, AIPermanentError, "the provider could not process the request"),
    ],
)
async def test_each_status_becomes_the_error_type_that_decides_whether_to_retry(
    wire: Wire, status_code: int, expected: type[Exception], reason: str
) -> None:
    """The mapping is the module's contract with `ai_service`, so it is pinned per status.

    A `5xx` is lumped with the retryable four rather than listed one by one because the rule —
    "the provider's own failure, try again" — is what a reader needs, and the fallback reason
    for a status with no sentence of its own is asserted here too. `404` is permanent with its
    own wording: a model that does not exist is a configuration answer, not a retry one.
    """
    wire.answers(
        {"error": {"code": "invalid_request_error", "message": "nope"}}, status_code=status_code
    )

    with pytest.raises(expected) as caught:
        await _provider().generate_embedding(TEXTS)

    assert caught.value.reason == reason


async def test_a_quota_exhausted_429_is_permanent_and_not_retried(wire: Wire) -> None:
    """**The one status this module reads differently from `groq.py`, and the reason is money.**

    A `429` normally means "slow down", and retrying is exactly right. `insufficient_quota`
    means the account is out of credit: it cannot succeed however long the caller waits, and
    treating it as transient would spend the whole attempt budget — three sleeps and three
    refused requests — to arrive at the failure it could have reported immediately.
    """
    wire.answers(
        {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}},
        status_code=429,
    )

    with pytest.raises(AIPermanentError) as caught:
        await _provider().generate_embedding(TEXTS)

    assert caught.value.reason == "the account has no remaining quota"


async def test_a_rate_limited_429_without_the_quota_code_is_still_transient(wire: Wire) -> None:
    """The other half of that comparison: the status is not overridden wholesale.

    A `rate_limit_exceeded` 429 is the ordinary throttle, and it has to stay retryable — a fix
    that made every 429 permanent would trade a wasted attempt budget for a lost ingestion.
    """
    wire.answers(
        {"error": {"code": "rate_limit_exceeded", "message": "Rate limit reached"}},
        status_code=429,
    )

    with pytest.raises(AITransientError):
        await _provider().generate_embedding(TEXTS)


async def test_the_providers_own_message_never_reaches_the_reason(wire: Wire) -> None:
    """The key and the documents are in the request; a provider's `message` can quote both.

    So the reason is looked up from a code we named, and this body is the one an attacker or a
    careless vendor would send: a 400 whose message echoes the credential and the text it was
    sent. None of it may appear in a string that reaches a log line or a stored row.
    """
    wire.answers(
        {
            "error": {
                "code": "invalid_request_error",
                "message": f"invalid api key {API_KEY} for input {TEXTS[0]!r}",
            }
        },
        status_code=400,
    )

    with pytest.raises(AIPermanentError) as caught:
        await _provider().generate_embedding(TEXTS)

    assert caught.value.reason == "the provider rejected the request"
    assert API_KEY not in caught.value.reason
    assert TEXTS[0] not in caught.value.reason


async def test_a_transport_failure_is_transient(wire: Wire) -> None:
    """A refused connection is the same failure class as a 503: nothing was refused *about* the
    request, so sending it again is a reasonable thing to do."""
    wire.fails(httpx.ConnectError("connection refused"))

    with pytest.raises(AITransientError) as caught:
        await _provider().generate_embedding(TEXTS)

    assert caught.value.reason == "the provider could not be reached"


async def test_a_timeout_says_so_rather_than_reporting_an_unreachable_provider(
    wire: Wire,
) -> None:
    """`TimeoutException` is a subclass of `TransportError`, so the order of the two checks in
    `_translate` is the difference between a true report and a plausible wrong one.

    They are different failures with different fixes — a timeout is a document too large or a
    model too slow, an unreachable provider is a network problem — so the message is asserted,
    not just the type.
    """
    wire.fails(httpx.ReadTimeout("the read timed out"))

    with pytest.raises(AITransientError) as caught:
        await _provider().generate_embedding(TEXTS)

    assert caught.value.reason == "the provider did not answer within the timeout"


# ---------------------------------------------------------------------------
# The answer that is not the shape we asked for
# ---------------------------------------------------------------------------


async def test_a_short_vector_list_is_an_output_error(wire: Wire) -> None:
    """Not a partial success. The caller writes these vectors against the passages it split a
    document into, so fewer vectors than texts means at least one passage would get another
    passage's meaning — and nothing downstream could detect it afterwards."""
    wire.answers(_response([_vector(0.1)], prompt_tokens=9))

    with pytest.raises(AIOutputError, match="returned 1 vectors for 2 texts") as caught:
        await _provider().generate_embedding(TEXTS)

    assert caught.value.prompt_tokens == 9


async def test_a_long_vector_list_is_an_output_error(wire: Wire) -> None:
    """The same check from the other side. A provider that echoed an extra vector would be
    answering a request nobody made, and silently dropping it is a habit worth not having."""
    wire.answers(_response([_vector(), _vector(), _vector()]))

    with pytest.raises(AIOutputError, match="returned 3 vectors for 2 texts"):
        await _provider().generate_embedding(TEXTS)


async def test_a_vector_of_the_wrong_width_is_an_output_error(wire: Wire) -> None:
    """A 3072-dimension vector is refused at validation rather than at the insert, where it
    would surface as a driver error against a 1536-dimension column.

    **The number is on the `__cause__` and not in the reason, and asserting that is the point.**
    `app/ai/provider.py`'s `_failure_summary` reports a rejected payload as `loc: type` and
    never a Pydantic `msg`, because a message can quote the input — so the reason says
    `did not match Embedding` and the chained `ValidationError` says `was 3072 wide`. Both
    halves are asserted here, and this test is what corrected two docstrings that claimed the
    reason named the dimension.

    The width comes from `app/models/knowledge_chunk.py`'s constant both here and in the
    validator, so a change of embedding model cannot leave one of the two checking the old
    number.
    """
    wire.answers(_response([_vector(width=3072), _vector()]))

    with pytest.raises(AIOutputError, match="did not match Embedding") as caught:
        await _provider().generate_embedding(TEXTS)

    # Two `from exc` links, and both are deliberate: `validate_output` chains the
    # `ValidationError`, and the provider re-raises with the token counts so `ai_service` can
    # price a call the vendor billed and this code rejected.
    validation_error = caught.value.__cause__.__cause__
    assert "was 3072 wide; the column is 1536" in str(validation_error)


async def test_a_body_that_is_not_json_is_an_output_error(wire: Wire) -> None:
    """A proxy's HTML error page, a truncated response — anything that is not the JSON we asked
    for is a provider that answered something unusable, which is `AIOutputError` rather than a
    retry or a crash."""
    wire.answers_raw("<html><body>502 Bad Gateway</body></html>")

    with pytest.raises(AIOutputError, match="the response was not JSON"):
        await _provider().generate_embedding(TEXTS)


async def test_a_response_without_a_data_key_is_an_output_error(wire: Wire) -> None:
    """`payload["data"]` is a chain of assumptions about a document we did not write, so a
    missing key becomes an empty list and then the count check — rather than a `TypeError`
    escaping as a 500."""
    wire.answers({"object": "list", "usage": {"prompt_tokens": 5}})

    with pytest.raises(AIOutputError, match="returned 0 vectors for 2 texts"):
        await _provider().generate_embedding(TEXTS)


async def test_the_tokens_ride_on_a_rejected_response(wire: Wire) -> None:
    """A response that was generated and then rejected was billed exactly like an accepted one.

    Without the counts on the exception the ledger would record a zero-cost row for a call the
    vendor charged for — `AIUsage`'s docstring is that a failed call *"is recorded rather than
    dropped"*, and this is the field that makes the record true.
    """
    wire.answers(_response([_vector()], prompt_tokens=42))

    with pytest.raises(AIOutputError) as caught:
        await _provider().generate_embedding(TEXTS)

    assert (caught.value.prompt_tokens, caught.value.completion_tokens) == (42, 0)


# ---------------------------------------------------------------------------
# No key
# ---------------------------------------------------------------------------


async def test_no_key_fails_at_the_point_of_use_and_not_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`EMBEDDING_API_KEY` is optional in `Settings` so a checkout with no key still starts.

    The failure therefore has to be loud where the key is used — a permanent error naming the
    variable, which is an operator's fix — rather than an import-time crash or, worse, a request
    sent with `Bearer None` that comes back as a 401 about a key nobody configured.
    """
    monkeypatch.setattr(openai_embedding, "_client", None)
    monkeypatch.setattr(openai_embedding, "_client_loop", None)
    monkeypatch.setattr(
        openai_embedding, "get_settings", lambda: SimpleNamespace(EMBEDDING_API_KEY=None)
    )

    with pytest.raises(AIPermanentError, match="EMBEDDING_API_KEY is not configured"):
        await _provider().generate_embedding(TEXTS)
