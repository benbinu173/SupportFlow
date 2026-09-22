"""The AI layer's error vocabulary — three answers to "what now?".

`app/core/mail.py` established this shape for the email boundary and this module restates
it rather than re-deciding it, because a caller has the same one question about a failed
provider call as about a failed send: **is this worth trying again?**

* `AITransientError` — the provider was unreachable, timed out, rate-limited us, or
  returned a 5xx. Retrying is the correct response, and `ai_service` does it with backoff.
* `AIPermanentError` — the request itself was refused: a bad key, no permission, an
  unknown model, a malformed request, a content-policy refusal. Retrying re-sends the same
  refused request, so the service does not.
* `AIOutputError` — the provider answered, and the answer was not the schema it was
  given. §18: *"The backend must validate the returned structure. Never assume LLM output
  is automatically valid."* This type is that sentence made structural: there is no path
  from a provider response to a caller's return value that does not pass through a
  Pydantic model.

**`AIOutputError` is a sibling, not a subclass of `AIPermanentError`, and the difference
is real.** Both stop the retry loop, but they are different reports: one says the provider
would not serve us, the other says the provider served us something unusable. The log line
and the operator's next action differ, so the types differ, and `ai_service` retries
`AITransientError` alone rather than "not the permanent one".

Nothing here carries the provider's own message. An SDK error can echo the request — and
the request carries the API key in a header and the customer's words in the body. So a
caller gets a short classification, and `app/ai/claude.py` logs the exception *type*.

**What a failure does carry is its token counts.** A call that was answered and then
rejected — truncated at `max_tokens`, or an answer that was not the schema — consumed
quota and was billed exactly like a successful one, and `AIUsage`'s own docstring is that
such a call *"is recorded rather than dropped"*. The counts ride on the exception because
the exception is the only thing that leaves the provider on that path; a caller that had
to ask a second time would be asking about a call that has already ended. Both default to
zero, which is what a request that never reached a model spent.

These are the layer's internal vocabulary. `app/core/exceptions.py`'s `AIServiceError` is
what a client sees, and `ai_service` is the one place the two meet.
"""


class AIError(Exception):
    """Base for every failure raised by `app/ai/`. Never escapes `ai_service` un-caught.

    `reason` is written for a log line, so it names the failure and never the payload:
    "the provider rejected the API key", not the key. Anything an operator needs beyond
    that is in the `error_type` the caller logs beside it.
    """

    def __init__(
        self,
        reason: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class AITransientError(AIError):
    """The call may succeed if tried again: unreachable, timed out, throttled, or 5xx."""


class AIPermanentError(AIError):
    """The call will fail identically every time: the request, key, or model is wrong."""


class AIOutputError(AIError):
    """The provider answered, and the answer could not be trusted.

    Covers every way a response can fail to be the requested schema: prose instead of a
    tool call, a tool call with the wrong tool, arguments that are not JSON, JSON missing a
    required field, a confidence outside `[0, 1]`, an enum member the model invented, and
    a reply truncated at `max_tokens` so the arguments were never closed.

    **Deliberately not retried.** The same input at the same temperature reproduces it,
    and §53 names "repeated AI calls" as something not to do: paying twice for the same
    refusal is the behaviour this type exists to prevent. A truncation is fixed by raising
    `AI_MAX_TOKENS`, which is a configuration answer and not a retry one.

    The message names the failure *shape* — "not a tool call", "confidence out of range" —
    and never the model's text, for the reason the module docstring gives.
    """
