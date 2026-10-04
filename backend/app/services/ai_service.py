"""The one call path — timeout, retry, validation, and the ledger, enforced once.

Spec §17 lists what the AI foundation has to implement: *"configuration, provider client,
structured output validation, timeout handling, retry strategy, usage tracking."* The first
two live in `app/ai/` and `app/core/config.py`. The other four are this module, and they are
here rather than in the provider because they are **policy** and policy applied per vendor
is policy with as many implementations as there are vendors.

**Four entry points, one core.** §17's four operations each get a function, because the
schema a caller wants is a property of the operation and the provider already knows it — a
generic `call(output=...)` would ask the caller to name a schema the provider cannot vary,
and would need a cast on the way in and another on the way out. `_run` is that generic
function, and the four public ones are three lines each over it.

**A failed call is still recorded.** `AIUsage`'s own docstring: *"A failed call still
consumed quota and may still have been billed, so it is recorded rather than dropped."*
Every attempt stages a row — success and failure alike — and `/analytics/overview` reports
`failed_calls` separately from `calls` so spend that bought nothing is visible rather than
folded into the total.

**The caller commits, including after a failure.** Rows are staged with `session.add` and
never committed here, because `ai_service` does not own the transaction — it is called from
inside one that a route or a task is already managing, and committing here would end that
transaction early. So the obligation travels with the call: **a caller that lets its own
rollback discard these rows loses exactly the record that matters most.** The walkthrough
demonstrates it by reading `failed_calls: 1` back over HTTP after a deliberate permanent
failure, rather than asserting that it happened.

**`was_cached` is `True` on exactly one kind of row, and it is the only row here that
records a call nobody made.** Every attempt that reached a provider is `False` — writing
`True` for one of those would corrupt the measurement the column exists for — and
`record_cache_hit` writes the other: §20's summary, served from the stored one because the
conversation had not moved, with zero tokens, zero cost and no call. It is a row rather than
a silence so that the saving is a count *beside* the calls it saved, and
`/analytics/overview` reports it as `cached_calls`.

**Provider errors become `AIServiceError` at this boundary.** §54 wants a stable error
surface, and the three internal types are not it. The distinction between transient,
permanent, and malformed survives in the log, where an operator can act on it, and not in
the response, where a client's only correct action is to try again either way.

**A caller passes a context, and there are exactly two kinds.** On the request path it is a
`TenantContext`, built in `app/api/deps.py` from the authenticated user. In a Celery task it
is a `WorkerContext`, built in `app/workers/ai_tasks.py` from the ticket's organization. Both
are accepted; neither can be spelled from a request body, and the tenant still comes from a
context in every case — §4's *"never trust organization_id supplied by the frontend"* is
unweakened. The two kinds differ in what they *say*, not in what they can carry: a
`WorkerContext` has no role and no permissions, so it cannot authorize anything, and these
functions do not ask it to.

**The four public functions return `AIResult[T]`, not `T`.** An `AIAnalysis` row records the
prompt and completion tokens a call was billed, and the only object that knows them is the
provider's `AIResult` — this module holds it and would otherwise discard it, leaving three
declared columns permanently empty. Callers read `.value`. The alternative, re-querying the
ledger row this function just staged, reads the ledger to learn something the service was
already holding.
"""

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.claude import ClaudeProvider
from app.ai.errors import AIError, AITransientError
from app.ai.fake import FakeProvider
from app.ai.groq import GroqProvider
from app.ai.pricing import cost_usd
from app.ai.provider import AIProvider, AIRequest, AIResult
from app.core.config import Settings, get_settings
from app.core.exceptions import AIServiceError
from app.core.tenancy import TenantContext, WorkerContext
from app.models.ai_usage import AIUsage
from app.models.enums import AIOperation
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)

logger = structlog.get_logger(__name__)

# The sleep between attempts, bound at module scope so tests can replace it. Retry timing
# is worth testing and worth testing without waiting: a suite that slept for real would
# take seconds per retry case and would be the slowest thing in the run.
_sleep = asyncio.sleep


def _provider() -> AIProvider:
    """The configured provider.

    Constructed per call rather than cached: none of the implementations holds anything. The
    HTTP client is cached inside the provider module at module scope, which is the object
    that owns a connection pool — this is one allocation.

    A test that wants a scripted provider patches this function, which is the seam
    `tests/unit/test_ai_retry.py` uses. `AI_PROVIDER=fake` is also honoured, but only in
    the test environment: `Settings` refuses that combination anywhere else (§60).

    The table is the list of providers that exist, so adding one is one entry here and one
    `Literal` member in `Settings` — and a value that somehow reached this line without an
    entry fails as a `KeyError` naming it, rather than as a silent default to somebody's
    vendor.
    """
    return _PROVIDERS[get_settings().AI_PROVIDER]()


#: Every implementation of `app/ai/provider.py`'s protocol, by the `AI_PROVIDER` value that
#: selects it. `fake` is the scripted one, and `Settings` is what keeps it out of a real
#: deployment — see the validator, not this comment.
_PROVIDERS: dict[str, Callable[[], AIProvider]] = {
    "anthropic": ClaudeProvider,
    "groq": GroqProvider,
    "fake": FakeProvider,
}


def _backoff_seconds(settings: Settings, attempt: int) -> float:
    """How long to wait after `attempt` failed, before the next one.

    Exponential from `AI_RETRY_BACKOFF_SECONDS`, jittered to between half and all of the
    ceiling. **Full jitter would be the textbook choice and is wrong here**: it can return
    a delay near zero, and the failure being retried is usually a rate limit, which is the
    one case where waiting less is pointless. A floor of half the ceiling keeps the intent
    — the provider gets real room to forget about us — while the variance still stops a
    hundred workers throttled by the same limit from returning in lockstep.
    """
    ceiling = settings.AI_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
    # Annotated because typeshed types `uniform` loosely; the product is a float.
    delay: float = random.uniform(0.5, 1.0) * ceiling  # noqa: S311 - desynchronising retries
    return delay


def _stage_usage(
    session: AsyncSession,
    context: TenantContext | WorkerContext,
    *,
    operation: AIOperation,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    was_successful: bool,
    ticket_id: uuid.UUID | None,
    was_cached: bool = False,
) -> None:
    """Append one ledger row for one attempt. **Never commits.**

    `organization_id` comes from the context and nowhere else. §4: *"Never trust
    organization_id supplied by the frontend"* — nothing in this function's signature could
    carry one even if a caller wanted to pass one, which is the point of taking a context
    rather than an id. The context may be a `WorkerContext`, which is the same guarantee
    expressed differently: it is built from a ticket's organization by a task, never from
    anything a client sent.

    Built here rather than through a repository, following `sla_tasks._record`: the ledger
    is append-only, written from exactly this one place, and a repository whose only method
    is `add` is ceremony. Reading it is `analytics_repository.ai_usage`, which already
    exists and needs no help.

    `ticket_id` and `user_id` are whatever the caller is attributing the call to, and both
    are nullable by design — a background embedding job has neither. Both are `SET NULL`
    foreign keys, so the row outlives the ticket it was about: spend that disappears when a
    ticket is deleted cannot be reconciled.

    `user_id` is `None` for a `WorkerContext` because there is no user. The agent who
    clicked "analyze" is recorded by the `AI_ANALYSIS_REQUESTED` audit row at request time,
    which is where §34 puts "who asked"; the ledger answers "which tenant spent what",
    which is a different question and one a task can answer.
    """
    session.add(
        AIUsage(
            organization_id=context.organization_id,
            operation=operation,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            # Priced here, from the table, at the moment of the call — which is what makes
            # a historical row keep the price that was actually charged when a rate changes.
            cost_usd=cost_usd(
                model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            ),
            ticket_id=ticket_id,
            user_id=context.user_id if isinstance(context, TenantContext) else None,
            latency_ms=latency_ms,
            was_successful=was_successful,
            was_cached=was_cached,
        )
    )


def record_cache_hit(
    session: AsyncSession,
    context: TenantContext | WorkerContext,
    *,
    operation: AIOperation,
    provider: str,
    model: str,
    ticket_id: uuid.UUID | None = None,
) -> None:
    """Append the ledger row for a call the cache answered. **Never commits.**

    §20's *"avoid regenerating the summary after every tiny message if unnecessary"* is
    answered by not making the call, and this is the row that records not making it: zero
    tokens, zero cost, zero latency, `was_cached=True`. **A row rather than a silence**,
    because `ai_usage.was_cached`'s own comment is that it exists *"so that the cache's
    actual saving can be measured instead of estimated"* — and an absence cannot be counted
    beside the calls it saved.

    **The saving is a count and not an amount, and that is the honest limit of what can be
    measured.** The tokens the call would have used are unknowable: it was never made. An
    estimate derived from the average call would be exactly the estimated number the column
    was added to replace, so `/analytics/overview` reports how many calls were free rather
    than a dollar figure invented for them.

    `provider` and `model` are the ones whose answer was reused, passed in by the caller
    rather than read from settings here — the same rule `AIAnalysis` states for the rows it
    stamps, because config changes and a ledger row has to keep naming the model it is a
    fact about. `ticket_id` is nullable for the reason every other staging path here has it
    nullable.
    """
    _stage_usage(
        session,
        context,
        operation=operation,
        provider=provider,
        model=model,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
        was_successful=True,
        ticket_id=ticket_id,
        was_cached=True,
    )


async def _run[T: BaseModel](
    session: AsyncSession,
    context: TenantContext | WorkerContext,
    *,
    operation: AIOperation,
    provider: AIProvider,
    call: Callable[[AIRequest], Awaitable[AIResult[T]]],
    request: AIRequest,
    ticket_id: uuid.UUID | None,
) -> AIResult[T]:
    """Call once, retry the retryable, ledger every attempt, and return the result.

    Returns the provider's `AIResult` rather than its `value` so the caller can record what
    the call cost — see the module docstring.

    The retry loop is the whole of §17's "timeout handling" and "retry strategy": the
    timeout is the client's (see `app/ai/claude.py`), and what this adds is the decision
    about what to do when it fires, which is the part that has to be in one place.

    **Only `AITransientError` is retried.** A permanent error would be re-sent identically,
    and an `AIOutputError` would be re-asked the same question at the same temperature to
    get the same unusable answer — §53 names "repeated AI calls" as waste, and paying twice
    for one malformed answer is precisely that. Both fail on the first attempt and the
    ledger says so.
    """
    settings = get_settings()
    model = settings.AI_MODEL

    for attempt in range(1, settings.AI_MAX_ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            result = await call(request)
        except AITransientError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            _stage_usage(
                session,
                context,
                operation=operation,
                provider=provider.name,
                model=model,
                prompt_tokens=exc.prompt_tokens,
                completion_tokens=exc.completion_tokens,
                latency_ms=latency_ms,
                was_successful=False,
                ticket_id=ticket_id,
            )
            if attempt == settings.AI_MAX_ATTEMPTS:
                logger.error(
                    "ai_call_failed",
                    operation=str(operation),
                    provider=provider.name,
                    model=model,
                    ticket_id=str(ticket_id) if ticket_id else None,
                    attempts=attempt,
                    reason=exc.reason,
                    retryable=True,
                )
                raise AIServiceError() from exc

            delay = _backoff_seconds(settings, attempt)
            logger.warning(
                "ai_call_retrying",
                operation=str(operation),
                provider=provider.name,
                model=model,
                attempt=attempt,
                max_attempts=settings.AI_MAX_ATTEMPTS,
                delay_seconds=round(delay, 3),
                reason=exc.reason,
            )
            await _sleep(delay)
        except AIError as exc:
            # Permanent, or output that could not be trusted. One attempt, one ledger row,
            # one log line that distinguishes the two by the exception type.
            latency_ms = int((time.perf_counter() - started) * 1000)
            _stage_usage(
                session,
                context,
                operation=operation,
                provider=provider.name,
                model=model,
                prompt_tokens=exc.prompt_tokens,
                completion_tokens=exc.completion_tokens,
                latency_ms=latency_ms,
                was_successful=False,
                ticket_id=ticket_id,
            )
            logger.error(
                "ai_call_failed",
                operation=str(operation),
                provider=provider.name,
                model=model,
                ticket_id=str(ticket_id) if ticket_id else None,
                attempts=attempt,
                reason=exc.reason,
                error_type=type(exc).__name__,
                retryable=False,
            )
            raise AIServiceError() from exc
        else:
            latency_ms = int((time.perf_counter() - started) * 1000)
            _stage_usage(
                session,
                context,
                operation=operation,
                provider=provider.name,
                model=model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=latency_ms,
                was_successful=True,
                ticket_id=ticket_id,
            )
            logger.info(
                "ai_call_succeeded",
                operation=str(operation),
                provider=provider.name,
                model=model,
                ticket_id=str(ticket_id) if ticket_id else None,
                attempt=attempt,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=latency_ms,
            )
            return result

    # `AI_MAX_ATTEMPTS` is bounded below at 1 in `Settings`, so the loop always either
    # returns or raises. This is unreachable and exists so the function has a return path
    # mypy can see rather than an implicit `None`.
    raise AIServiceError()


async def classify_ticket(
    session: AsyncSession,
    context: TenantContext | WorkerContext,
    request: AIRequest,
    *,
    ticket_id: uuid.UUID | None = None,
) -> AIResult[Classification]:
    """§18 — classify a ticket into a category, a subcategory, a priority, and a confidence.

    Returns the whole `AIResult`: `.value` is the `Classification`, and the token counts
    beside it are what `ai_analysis_service` writes onto the `AIAnalysis` row it is filling.

    The caller commits the ledger row this stages, including when it raises.
    """
    provider = _provider()
    return await _run(
        session,
        context,
        operation=AIOperation.CLASSIFY,
        provider=provider,
        call=provider.classify_ticket,
        request=request,
        ticket_id=ticket_id,
    )


async def analyze_sentiment(
    session: AsyncSession,
    context: TenantContext | WorkerContext,
    request: AIRequest,
    *,
    ticket_id: uuid.UUID | None = None,
) -> AIResult[SentimentResult]:
    """§19 — read the customer's sentiment and how confident that reading is.

    The caller commits the ledger row this stages, including when it raises.
    """
    provider = _provider()
    return await _run(
        session,
        context,
        operation=AIOperation.SENTIMENT,
        provider=provider,
        call=provider.analyze_sentiment,
        request=request,
        ticket_id=ticket_id,
    )


async def summarize_conversation(
    session: AsyncSession,
    context: TenantContext | WorkerContext,
    request: AIRequest,
    *,
    ticket_id: uuid.UUID | None = None,
) -> AIResult[ConversationSummary]:
    """§20 — summarize a conversation.

    The caller commits the ledger row this stages, including when it raises.
    """
    provider = _provider()
    return await _run(
        session,
        context,
        operation=AIOperation.SUMMARIZE,
        provider=provider,
        call=provider.summarize_conversation,
        request=request,
        ticket_id=ticket_id,
    )


async def generate_response(
    session: AsyncSession,
    context: TenantContext | WorkerContext,
    request: AIRequest,
    *,
    ticket_id: uuid.UUID | None = None,
) -> AIResult[SuggestedReply]:
    """§21 — draft a reply for an agent to review, edit, and send themselves.

    **Nothing here sends anything.** §21's *"AI must NEVER automatically send a
    customer-facing response in the default implementation"* is enforced by this function
    returning a draft: the return type is a `SuggestedReply` and there is no code path from
    it to a message row. §41's "AI-generated" distinction is `SenderType.AI_DRAFT`, which
    the caller sets when it stores one.

    The caller commits the ledger row this stages, including when it raises.
    """
    provider = _provider()
    return await _run(
        session,
        context,
        operation=AIOperation.SUGGEST_RESPONSE,
        provider=provider,
        call=provider.generate_response,
        request=request,
        ticket_id=ticket_id,
    )
