"""A scripted provider — for tests, and refused everywhere else.

ADR-008: *"A deterministic fake provider exists for tests only."* §60 is more direct about
why: the final implementation must not *"use fake AI results"*. So this class exists, and
`Settings` refuses to build a configuration that selects it outside `ENVIRONMENT == "test"`
— the guard is in configuration, not in a comment, and `tests/unit/test_config.py` asserts
the refusal rather than trusting it.

**Why a fake at all, when the real provider works.** §46 names four cases that have to be
testable: a valid structured response, a malformed one, a provider failure, and retry
behaviour. Three of those are *outcome* questions and none of them is about Anthropic. A
test that reaches the network to ask what happens when a call is rate-limited twice and
then succeeds is slow, flaky, costs money, and cannot be run in CI at all. Scripting the
outcomes makes each of the four a unit test.

**It validates, rather than returning what it was handed.** An outcome that is a raw dict
or a string goes through the same `validate_output` the real provider uses, so
`AIOutputError` is produced by the same code in test and in production — a fake that
skipped validation would make the malformed-output test a test of the fake.

**The call count is the assertion.** "A permanent error does not retry" is not a claim
about a return value; it is a claim about how many times the provider was asked. So
`calls` is public and the retry test reads it, which is the only way that property can be
checked at all.
"""

from collections.abc import Sequence

from pydantic import BaseModel

from app.ai.errors import AIError
from app.ai.provider import AIRequest, AIResult, validate_output
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)


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
