"""Scripted providers — for tests, and refused everywhere else.

ADR-008: *"A deterministic fake provider exists for tests only."* §60 is more direct about
why: the final implementation must not *"use fake AI results"*. So these classes exist, and
`Settings` refuses to build a configuration that selects either of them outside
`ENVIRONMENT == "test"` — the guard is in configuration, not in a comment, and
`tests/unit/test_config.py` asserts the refusal rather than trusting it.

**Why a fake at all, when the real provider works.** §46 names four cases that have to be
testable: a valid structured response, a malformed one, a provider failure, and retry
behaviour. Three of those are *outcome* questions and none of them is about Anthropic. A
test that reaches the network to ask what happens when a call is rate-limited twice and
then succeeds is slow, flaky, costs money, and cannot be run in CI at all. Scripting the
outcomes makes each of the four a unit test.

**There are two fakes because §17 has two protocols.** `FakeProvider` answers the five
generation operations from a script of outcomes. `FakeEmbeddingProvider` answers §22's
embedding call, and the two are as separate here as `EmbeddingProvider` and `AIProvider`
are in `app/ai/provider.py` — a single fake implementing both would be a class held together
by nothing but the fact that tests use it.

**They validate, rather than returning what they were handed.** An outcome that is a raw dict
or a string goes through the same `validate_output` the real provider uses, so
`AIOutputError` is produced by the same code in test and in production — a fake that
skipped validation would make the malformed-output test a test of the fake.

**The call count is the assertion.** "A permanent error does not retry" is not a claim
about a return value; it is a claim about how many times the provider was asked. So
`calls` is public and the retry test reads it, which is the only way that property can be
checked at all.
"""

import hashlib
import random
from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from app.ai.errors import AIError
from app.ai.provider import AIRequest, AIResult, validate_output
from app.models.knowledge_chunk import EMBEDDING_DIMENSIONS
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)
from app.schemas.knowledge import Embedding, KnowledgeAnswer


class FakeProvider:
    """`AIProvider` that answers from a script.

    Each outcome is one call, in order:

    * an `AIError` — raised, so a transient/permanent failure can be placed at any point;
    * a `BaseModel`, a `dict`, or a JSON string — validated into the requested schema, so
      a well-formed answer and a malformed one are the same kind of script entry.

    A call past the end of the script raises `AssertionError` rather than inventing an
    outcome: a test that calls the provider more times than it scripted has a bug, and the
    count is usually the thing under test.
    """

    name = "fake"

    def __init__(
        self,
        *outcomes: object,
        prompt_tokens: int = 1_200,
        completion_tokens: int = 80,
    ) -> None:
        # Non-zero by default so a ledger written from a fake call has real numbers in it
        # — `cost_usd` is a product, and a test that asserted on zero would pass even if
        # pricing were never called.
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._outcomes: Sequence[object] = outcomes
        self.calls = 0
        #: Every request the provider was handed, so a test can assert on what `ai_service`
        #: assembled without reaching into the service's internals.
        self.requests: list[AIRequest] = []

    def _answer[T: BaseModel](self, request: AIRequest, output: type[T]) -> AIResult[T]:
        index = self.calls
        self.calls += 1
        self.requests.append(request)

        if index >= len(self._outcomes):
            raise AssertionError(
                f"FakeProvider was called {index + 1} times but only "
                f"{len(self._outcomes)} outcomes were scripted"
            )

        outcome = self._outcomes[index]
        if isinstance(outcome, AIError):
            raise outcome

        return AIResult(
            value=validate_output(output, outcome),
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
        )

    async def classify_ticket(self, request: AIRequest) -> AIResult[Classification]:
        return self._answer(request, Classification)

    async def analyze_sentiment(self, request: AIRequest) -> AIResult[SentimentResult]:
        return self._answer(request, SentimentResult)

    async def summarize_conversation(self, request: AIRequest) -> AIResult[ConversationSummary]:
        return self._answer(request, ConversationSummary)

    async def generate_response(self, request: AIRequest) -> AIResult[SuggestedReply]:
        return self._answer(request, SuggestedReply)

    async def answer_question(self, request: AIRequest) -> AIResult[KnowledgeAnswer]:
        return self._answer(request, KnowledgeAnswer)


class FakeEmbeddingProvider:
    """`EmbeddingProvider` that answers from a script, or from the text's own hash.

    **Two modes, and a test picks one by whether it passes `vectors`** — because the two
    answer questions that need different things from a vector, and a single mode would be a
    double pretending that a hash is a meaning:

    * **Hashed** (`FakeEmbeddingProvider()`) — every text is embedded by a deterministic
      function of its own bytes. Stable across runs, correct in width, and *semantically
      meaningless*: two paraphrases are no nearer each other than two unrelated sentences.
      That is the right double for everything about ingestion — the rows were written, in
      order, one vector per chunk, with the model's name on them — and the wrong one for
      anything about retrieval, where a test would be asserting a property the double cannot
      have.
    * **Scripted** (`FakeEmbeddingProvider({"some text": vector})`) — a text in the mapping
      gets the vector the test chose, which is how a test makes similarity a *decision*:
      give the question and one passage the same vector and that passage is the nearest
      neighbour by construction, whatever the threshold is.

    **A scripted text that is not in the mapping raises**, rather than falling back to the
    hash. A test that scripted some of its texts and not others has a bug, and a silent
    fallback is the kind that turns a ranking assertion into a coincidence — the same reason
    `FakeProvider` raises instead of inventing an outcome past the end of its script.
    """

    name = "fake"

    def __init__(self, vectors: Mapping[str, Sequence[float]] | None = None) -> None:
        #: `None` is the hashed mode; a mapping is the scripted one. Held as `None` rather
        #: than an empty dict so the two modes cannot be confused for each other.
        self._vectors = None if vectors is None else dict(vectors)
        #: Every text this provider was asked to embed, in order — so a test can assert what
        #: `ai_service` sent without reaching into the service, exactly as `FakeProvider.
        #: requests` is used for the same reason.
        self.texts: list[str] = []
        #: How many times the provider was called. The "no published chunks means no
        #: embedding call" assertion in `tests/integration/test_knowledge_draft_grounding.py`
        #: is a claim about this being zero, which no return value can express.
        self.calls = 0

    def _vector_for(self, text: str) -> list[float]:
        """The scripted vector for `text`, or a stable pseudo-vector derived from it."""
        if self._vectors is None:
            # A seed taken from the text's own digest, so the same text yields the same
            # vector on every run and on every machine — which is what makes a hashed-mode
            # test deterministic without pinning 1536 literal floats in it.
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
            rng = random.Random(seed)  # noqa: S311 - a test double, not a secret
            return [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIMENSIONS)]

        try:
            return [float(value) for value in self._vectors[text]]
        except KeyError as exc:
            raise AssertionError(
                f"FakeEmbeddingProvider was scripted with {len(self._vectors)} texts and "
                f"none of them is this one ({len(text)} characters, starting "
                f"{text[:40]!r}); pass no arguments to embed by hash instead"
            ) from exc

    async def generate_embedding(self, texts: list[str]) -> AIResult[Embedding]:
        """One vector per text, in the order they were given — the protocol's contract.

        `prompt_tokens` is estimated from the texts by the same 4-characters-per-token ratio
        `app/services/document_text.py` uses, rather than being a constant: a fake whose
        ledger row reports the same token count for a 3-chunk document and a 300-chunk one
        would make a test that asserted on the ledger pass by not asking much. Non-zero by
        construction, so `cost_usd` is exercised on every call.
        """
        self.calls += 1
        self.texts.extend(texts)
        return AIResult(
            value=Embedding(vectors=[self._vector_for(text) for text in texts]),
            prompt_tokens=sum(max(1, len(text) // 4) for text in texts),
            # Nothing was generated, which is a fact about embeddings and not a placeholder.
            completion_tokens=0,
        )
