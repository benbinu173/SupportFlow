"""Ticket analysis — §18's pipeline, from "queue it" to "tell somebody".

Spec §18 writes the flow out in nine steps, and this module is the six that come after the
ticket exists: queue the analysis, call the provider, validate, store the result, update the
ticket, write the timeline, notify, and publish. `app/services/ai_service.py` owns the call
and the ledger; `app/ai/provider.py` owns the validation; this owns the arrangement.

**Two entry points, two processes.** `request_analysis` runs inside a request — it writes
the rows, stages the audit entry, commits, and hands a task to a broker. `run_analysis` runs
inside a Celery task, in a different process with its own session, and does the work. They
are deliberately apart: the request path must not wait for a language model (§16: *"The API
should not wait unnecessarily for the LLM"*), and nothing here calls a provider from a
route. The only thing that crosses between them is a ticket id, an organization id, and the
ids of the rows the first one wrote.

**A row exists before the work does.** `request_analysis` writes one `pending` row per
operation and *then* queues. `AIAnalysis`'s own docstring gives the reason — *"a pending or
failed analysis is visible rather than silently absent"* — and writing the rows first is
what makes it true: a client that asks for an analysis and reads back immediately sees two
`pending` rows rather than an empty list, and a client that reads back after a broker outage
sees them too.

**The analysis is not the ticket, and the line between them is §6.** The model owns the
descriptive fields — `category`, `subcategory`, `sentiment` and their confidences — and the
recommendation, which lands in `ai_recommended_priority`. It does **not** own `tickets.priority`.
That is the business decision, `ticket_service.change_priority` is its only writer, and the
two columns exist separately so the recommendation survives the override. A worker that
applied its own suggestion would make §6's comparison vacuous.

**One task, not one per operation.** Classification and sentiment are two rows but one act,
and §18's last two steps are singular — the ticket gets one timeline entry, one
notification, and one announcement. Two tasks would each independently decide to publish,
which is how a client ends up re-reading the same ticket twice and an agent gets two
alerts for one arrival.

**A failure is contained to its own operation.** §7's *"AI provider failure degrades
gracefully"* means a sentiment call that fails must not lose a classification that
succeeded: each operation is caught separately, its row is marked `failed` with the reason,
and the rest of the run continues. Nothing is retried here — retry is `ai_service._run`'s,
and a second retry loop would multiply the two policies.
"""

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.provider import AIRequest, AIResult
from app.core import cache, redis
from app.core.config import get_settings
from app.core.exceptions import AIServiceError
from app.core.tenancy import RequestOrigin, TenantContext, WorkerContext
from app.models.ai_analysis import AIAnalysis
from app.models.enums import (
    AIOperation,
    AuditAction,
    ProcessingStatus,
    TicketEventType,
)
from app.models.notification import Notification
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.repositories import ai_repository
from app.schemas.ai import Classification, SentimentResult
from app.services import ai_service, audit_service, notification_service
from app.websocket import manager as realtime

logger = structlog.get_logger(__name__)

#: The operations one "analyze this ticket" request performs. §51's list, minus the parts
#: later phases own: summarization is §20 and Phase V, suggested replies are §21 and Phase W,
#: and the knowledge base is §22 and Phase X. The tuple is ordered because the rows are
#: written in this order and a client reading them back sees it.
ANALYSIS_OPERATIONS: tuple[AIOperation, ...] = (
    AIOperation.CLASSIFY,
    AIOperation.SENTIMENT,
)

#: The instruction each operation is called with. A dict rather than a branch, so
#: `_request_for` cannot be reached with an operation that has no instruction — it raises a
#: `KeyError` naming the operation, which is the failure a reader would want.
_INSTRUCTIONS: dict[AIOperation, str] = {
    AIOperation.CLASSIFY: prompts.CLASSIFICATION_INSTRUCTION,
    AIOperation.SENTIMENT: prompts.SENTIMENT_INSTRUCTION,
}

#: What the fenced block is called in the prompt. **The caller's own words**, per
#: `app/ai/prompts.py`'s rule: the label is prose the model reads as instruction, so it must
#: not be anything a customer wrote. "The customer's ticket" describes the block; the ticket's
#: subject would be part of it.
_CONTENT_LABEL = "the customer's support ticket"

#: Stored on a row whose operation this build has no implementation for. See `_execute`.
_NO_IMPLEMENTATION = "This analysis operation is not implemented in this version."

#: The status pair that means "queued or running". `AIAnalysis.status` has four members and
#: this names the two that are not an outcome, which is the distinction every guard below
#: actually needs — a row in `PROCESSING` has been claimed, and a row in `COMPLETED` or
#: `FAILED` is finished and must not be run again.
_TERMINAL: frozenset[ProcessingStatus] = frozenset(
    {ProcessingStatus.COMPLETED, ProcessingStatus.FAILED}
)


@dataclass(frozen=True)
class _Outcome:
    """One operation's result, reduced to what the analysis row stores.

    Built by `_reduced` from the provider's `AIResult`, and it exists because the two
    schemas have no common supertype beyond `BaseModel` — the row is written from this
    rather than from a union the type checker cannot narrow.
    """

    confidence: float
    payload: dict[str, object]
    prompt_tokens: int
    completion_tokens: int


# ---------------------------------------------------------------------------
# The request path
# ---------------------------------------------------------------------------


async def request_analysis(
    session: AsyncSession,
    context: TenantContext,
    ticket: Ticket,
    *,
    origin: RequestOrigin | None = None,
) -> list[AIAnalysis]:
    """Queue this ticket's analysis and return the rows that describe it. **Commits.**

    The `Ticket` and not a ticket id, following `notification_service.notify_for_event`. Two
    callers reach this and both have one: the route resolves it through
    `require_visible_ticket` before the capability check can matter, and `create_ticket`
    passes the ticket it just wrote. Taking an id would mean resolving it again here — a
    second query for the first caller and a wasted one for the second — and would give this
    function an authorization decision to make that its callers have already made.

    **The audit row is written here and not in the route.** §34 lists
    `AI_ANALYSIS_REQUESTED`, and this is the moment the request happens; a route that
    audited and then failed to queue would leave a trail claiming an analysis that was never
    requested. `origin` is passed through for the same reason every other audited call site
    passes it: the trail answers "where did this come from", and this function does not know.

    **Nothing in flight is queued twice.** §16 asks to avoid duplicate processing, and the
    one way a duplicate is easy to ask for is a user pressing the button twice: without the
    `in_flight_analyses` check below, two clicks are four provider calls for a question
    already on its way, and §53 names repeated AI calls as waste. A request that finds
    everything already in flight creates no rows, writes no audit entry, and queues nothing,
    returning what is already there — which is the honest answer to "analyze this", and the
    same rows the caller would have got the first time.

    The commit is this function's, unlike `ai_service`'s staging: the rows have to be
    readable before the task that fills them can find them, and `enqueue_analysis` runs
    after it for the reason `enqueue_delivery` documents.
    """
    in_flight = await ai_repository.in_flight_analyses(session, ticket=ticket)
    covered = {analysis.operation for analysis in in_flight}

    settings = get_settings()
    created = [
        AIAnalysis(
            organization_id=context.organization_id,
            ticket_id=ticket.id,
            operation=operation,
            status=ProcessingStatus.PENDING,
            # Stamped at queue time, not read from config when the row is displayed: config
            # changes, and a historical analysis must still name the model that was asked.
            # The worker overwrites neither — `ai_service` reads the same settings when it
            # makes the call, so the two agree unless a deployment changed under way, which
            # is exactly the case worth being able to see.
            provider=settings.AI_PROVIDER,
            model=settings.AI_MODEL,
        )
        for operation in ANALYSIS_OPERATIONS
        if operation not in covered
    ]

    if not created:
        logger.info(
            "ai_analysis_already_queued",
            ticket_id=str(ticket.id),
            organization_id=str(context.organization_id),
            operations=sorted(str(analysis.operation) for analysis in in_flight),
        )
        return list(in_flight)

    for analysis in created:
        session.add(analysis)

    audit_service.record_for(
        session,
        context,
        AuditAction.AI_ANALYSIS_REQUESTED,
        target_type="ticket",
        target_id=ticket.id,
        metadata={"operations": [str(analysis.operation) for analysis in created]},
        origin=origin,
    )
    await session.commit()

    enqueue_analysis(
        ticket.id,
        context.organization_id,
        analysis_ids=[analysis.id for analysis in created],
    )

    logger.info(
        "ai_analysis_queued",
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        operations=[str(analysis.operation) for analysis in created],
        actor_id=str(context.user_id),
    )
    return [*in_flight, *created]


def enqueue_analysis(
    ticket_id: uuid.UUID, organization_id: uuid.UUID, *, analysis_ids: Sequence[uuid.UUID]
) -> int:
    """Hand one analysis to the broker. **Call this after the commit.**

    Synchronous, and after the commit for the reason `enqueue_delivery` gives: the task's
    first act is to read the rows it was handed the ids of, and a task that started before
    those rows were visible would find nothing and quietly do nothing.

    **A broker that is down does not fail the request.** The rows are committed and the
    timeline will show the analysis as `pending`, so the user's action succeeded; only the
    work is late, and a worker or a retry picks it up. The alternative is a 500 for a ticket
    that was created, which is a worse answer than a queue that is behind. The exception is
    logged by *type* and without a traceback, following `enqueue_delivery`: a connection
    error's message embeds the broker URL, and in production that URL carries a password
    (§4 — no secrets in logs).

    **The rows are left `pending` and not marked failed**, which is a deliberate choice and
    the opposite of what `run_analysis` does with a provider failure. A failed provider call
    is an answer about the model, and recording it as a `failed` analysis is accurate. A
    broker that is down is an answer about the infrastructure, and marking the row `failed`
    would claim the model said nothing when it was never asked — and would discourage the
    retry that is the correct response. `ix_ai_analyses_pending` is partial on exactly this
    status pair, so a stuck row is visible to the query that was built to find it.

    Returns how many were queued, which is `0` or `1` — the shape `enqueue_delivery` uses,
    so a caller or a test can tell "nothing to send" from "queued and forgotten".
    """
    if not analysis_ids:
        return 0

    # Imported here rather than at module scope so the API process does not import the task
    # module on every startup, following `enqueue_delivery`. Nothing above this line needs
    # Celery to exist, and the dependency stays visible at the one place it is used.
    from app.workers.ai_tasks import analyze_ticket

    try:
        analyze_ticket.delay(
            str(ticket_id),
            str(organization_id),
            [str(analysis_id) for analysis_id in analysis_ids],
        )
    except Exception as exc:
        logger.warning(
            "ai_analysis_not_queued",
            ticket_id=str(ticket_id),
            organization_id=str(organization_id),
            error_type=type(exc).__name__,
        )
        return 0
    return 1


# ---------------------------------------------------------------------------
# The worker path
# ---------------------------------------------------------------------------


async def run_analysis(
    session: AsyncSession,
    context: WorkerContext,
    *,
    ticket_id: uuid.UUID,
    analysis_ids: Sequence[uuid.UUID],
) -> dict[str, int]:
    """Run one ticket's queued analyses. **Commits, and publishes after committing.**

    The whole of the worker's work in one call, so `app/workers/ai_tasks.py` is a task
    decorator and an event loop and nothing else — the same arrangement
    `sla_tasks._sweep` has, and for the same reason: the logic is testable against a real
    database without a broker, which is how `tests/integration/test_ai_analysis.py` runs it.

    **Two commits, and they are not interchangeable.** The first flips the rows to
    `PROCESSING` before any provider call, so "running" is a state another process can see
    rather than one that exists only inside this task — that is what makes a second delivery
    of the same task a no-op instead of a second provider call, which matters because the
    task is declared with `acks_late` and at-least-once delivery is the contract. The second
    commits the results, the ticket's new fields, the timeline entry, the notification, and
    every ledger row the calls staged.

    **The ledger rows are committed even when every operation failed.** `ai_service`'s
    docstring is explicit that a caller which lets its own rollback discard them *"loses
    exactly the record that matters most"*, and a failed call is still spend.

    **Nothing is announced when nothing succeeded.** §18's last two steps describe a finished
    analysis: a timeline entry saying one completed, and an alert to the agent who will
    review it. When both operations fail there is no result to review and no change to the
    ticket, so writing the entry would be a false claim on the record and sending the alert
    would be an interruption about nothing. The rows say `failed` and `error_message` says
    why, which is where somebody asking "what happened to my analysis" should be looking.
    """
    counts = {"completed": 0, "failed": 0, "skipped": 0}

    ticket = await ai_repository.load_ticket(session, context.organization_id, ticket_id)
    if ticket is None:
        # Deleted between the queue and the run. `ai_analyses.ticket_id` is `ON DELETE
        # CASCADE`, so the rows the task was handed are gone too and there is nothing to
        # report — this is a normal outcome and not an error.
        logger.warning(
            "ai_analysis_ticket_missing",
            ticket_id=str(ticket_id),
            organization_id=str(context.organization_id),
        )
        return counts

    analyses = await ai_repository.load_analyses(
        session, context.organization_id, analysis_ids=analysis_ids
    )

    # Anything already finished is work that has been done. This is §16's "avoid duplicate
    # processing" and it is what makes a redelivered task safe: a row only ever leaves
    # `pending` once, and a second delivery finds nothing to claim.
    pending = [analysis for analysis in analyses if analysis.status not in _TERMINAL]
    counts["skipped"] = len(analyses) - len(pending)
    if not pending:
        return counts

    for analysis in pending:
        analysis.status = ProcessingStatus.PROCESSING
    await session.commit()

    completed_this_run: list[AIAnalysis] = []
    for analysis in pending:
        started = time.perf_counter()
        try:
            outcome = await _execute(session, context, analysis, ticket)
        except AIServiceError as exc:
            latency_ms = _elapsed_ms(started)
            _fail(analysis, message=exc.message, latency_ms=latency_ms)
            counts["failed"] += 1
            logger.error(
                "ai_analysis_failed",
                ticket_id=str(ticket.id),
                organization_id=str(context.organization_id),
                analysis_id=str(analysis.id),
                operation=str(analysis.operation),
            )
            continue

        latency_ms = _elapsed_ms(started)
        if outcome is None:
            _fail(analysis, message=_NO_IMPLEMENTATION, latency_ms=latency_ms)
            counts["failed"] += 1
            continue

        _complete(analysis, outcome, latency_ms=latency_ms)
        completed_this_run.append(analysis)
        counts["completed"] += 1

    event: TicketEvent | None = None
    notifications: list[Notification] = []
    if completed_this_run:
        event = _record_completion(
            session, context.organization_id, ticket, completed=completed_this_run
        )
        notifications = await notification_service.notify_analysis_completed(
            session, organization_id=context.organization_id, ticket=ticket
        )

    await session.commit()

    # After the commit, and outside the session, like every other producer in this codebase.
    # `scoped_client` and not `get_client`: `event_loop.run` builds *and closes* a loop per
    # task invocation, so a client cached on the shared module would be bound to a loop that
    # no longer exists by the next task. See `app/core/redis.py`.
    #
    # Both calls never raise, so a Redis outage cannot turn a completed analysis into a
    # failed task that Celery retries — the rows are the durable record and they are already
    # written.
    async with redis.scoped_client() as client:
        if event is not None:
            await realtime.publish(ticket, event, notifications, client=client)
        else:
            await realtime.publish_notifications(notifications, client=client)
        # The AI fields on the ticket are part of what every cached analytics figure is
        # computed over — `ai_usage` totals and the recommended-priority distribution both
        # move here, so the tenant's cached entries are invalidated in the same
        # post-commit position the ticket writers use.
        await cache.invalidate(context.organization_id, client=client)

    logger.info(
        "ai_analysis_complete",
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        **counts,
    )
    return counts


async def _execute(
    session: AsyncSession,
    context: WorkerContext,
    analysis: AIAnalysis,
    ticket: Ticket,
) -> _Outcome | None:
    """Make one operation's call and copy its result onto the ticket.

    Returns `None` for an operation this build has no implementation for, which the caller
    records as a failure. That case is unreachable while `ANALYSIS_OPERATIONS` is what
    `request_analysis` writes rows for — and reachable the moment it is not, because a task
    is handed ids over a broker and a deployment mid-rollout can deliver a row from a
    version that knew an operation this one does not. Failing the row says so on the ticket
    where a person will see it, rather than dying in a worker log.

    **The ticket is written here, per operation, and not from the row afterwards.** The
    mapping from a validated model to the columns it feeds is the one place this module and
    `app/schemas/ai.py` have to agree, and writing it as two typed branches means a field
    renamed on one side is a mypy error rather than a column that silently stops being
    written.
    """
    if analysis.operation is AIOperation.CLASSIFY:
        classified = await ai_service.classify_ticket(
            session, context, _request_for(analysis.operation, ticket), ticket_id=ticket.id
        )
        classification = classified.value
        ticket.category = classification.category
        ticket.subcategory = classification.subcategory
        # The recommendation, and never `ticket.priority` — see the module docstring. §6
        # keeps the model's suggestion and the business decision in separate columns so the
        # two can be compared, and the comparison is worthless if the worker fills in both.
        ticket.ai_recommended_priority = classification.priority
        ticket.ai_classification_confidence = classification.confidence
        return _reduced(classified)

    if analysis.operation is AIOperation.SENTIMENT:
        analyzed = await ai_service.analyze_sentiment(
            session, context, _request_for(analysis.operation, ticket), ticket_id=ticket.id
        )
        sentiment = analyzed.value
        ticket.sentiment = sentiment.sentiment
        ticket.sentiment_confidence = sentiment.confidence
        return _reduced(analyzed)

    logger.error(
        "ai_operation_unimplemented",
        ticket_id=str(ticket.id),
        analysis_id=str(analysis.id),
        operation=str(analysis.operation),
    )
    return None


def _request_for(operation: AIOperation, ticket: Ticket) -> AIRequest:
    """The call for one operation against one ticket.

    `max_tokens` comes from settings through this function rather than from a per-provider
    default, for the reason `AIRequest`'s docstring gives: it has one home.

    The content is the ticket's own words through `prompts.ticket_content`, and the fence
    around it is applied by the provider — this function never touches the markers, which is
    what keeps the mechanism single.
    """
    return AIRequest(
        instruction=_INSTRUCTIONS[operation],
        content=prompts.ticket_content(ticket.subject, ticket.description),
        content_label=_CONTENT_LABEL,
        max_tokens=get_settings().AI_MAX_TOKENS,
    )


def _reduced(result: AIResult[Classification] | AIResult[SentimentResult]) -> _Outcome:
    """One provider result reduced to what the analysis row stores.

    **The model's own dump, re-serialized, and the token counts untouched.** The payload is
    `mode="json"` so the enums become their wire values before they reach JSONB — a
    `StrEnum` would serialize correctly through `json.dumps` anyway, and the mode makes that
    a property of this function rather than of every future field.

    Both schemas carry a `confidence`, which is why the union above needs no narrowing to
    read it: the attribute access is checked against both members.
    """
    value = result.value
    return _Outcome(
        confidence=value.confidence,
        payload=value.model_dump(mode="json"),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )


def _complete(analysis: AIAnalysis, outcome: _Outcome, *, latency_ms: int) -> None:
    """Write a finished row. **Never commits.**

    Everything arrives at once — the payload, the confidence, the tokens, and the timestamp
    — so a `completed` row is never half-written, and the `terminal_status_has_payload`
    constraint is satisfied on the same statement that sets the status.

    `latency_ms` here is the wall time of the whole operation, retries included, which is a
    different question from the ledger's per-attempt figure: this one answers "how long did
    the user wait", and the ledger answers "how long did each call take". Both are worth
    having and neither can be derived from the other.
    """
    analysis.status = ProcessingStatus.COMPLETED
    analysis.result = outcome.payload
    analysis.confidence = outcome.confidence
    analysis.prompt_tokens = outcome.prompt_tokens
    analysis.completion_tokens = outcome.completion_tokens
    analysis.latency_ms = latency_ms
    analysis.completed_at = datetime.now(UTC)


def _fail(analysis: AIAnalysis, *, message: str, latency_ms: int) -> None:
    """Write a failed row. **Never commits.**

    `message` is the sentence `AIServiceError` carries, not the provider's own text, and
    that is the security boundary showing through rather than a missing detail. The column's
    comment says it holds a *"provider-side message"*, and `AIServiceError`'s docstring says
    why no such string reaches a caller: *"an SDK error can quote the request, and the
    request carries the API key in a header and the customer's words in the body."* What is
    stored is therefore the safe sentence staff can be shown, and the reason — transient,
    permanent, or malformed — is in the `ai_call_failed` log line where an operator can act
    on it.

    No token counts: the attempt's figures are on its ledger row, and copying a failed
    attempt's tokens here would make a row that produced nothing look like it produced
    something.
    """
    analysis.status = ProcessingStatus.FAILED
    analysis.error_message = message
    analysis.latency_ms = latency_ms
    analysis.completed_at = datetime.now(UTC)


def _record_completion(
    session: AsyncSession,
    organization_id: uuid.UUID,
    ticket: Ticket,
    *,
    completed: Sequence[AIAnalysis],
) -> TicketEvent:
    """Append the completion to the ticket's timeline. **Never commits.**

    `completed` is the rows *this run* finished, not every row the task loaded — a row that
    was already terminal when the task arrived was somebody else's work, and naming it here
    would credit this run with it.

    Built here rather than through `ticket_service.record_event`, following
    `sla_tasks._record` — that function needs a `TenantContext` to name its actor, and there
    is no actor. `actor_user_id` is left `None`, which is the state the column's own comment
    describes: *"NULL when the system acted rather than a person — SLA breaches and completed
    AI analyses have no actor."*

    **`extra_data` names what ran and not what it found.** The results are on the ticket's own
    columns and on the `ai_analyses` rows, both of which are reachable from this entry's
    ticket — so copying them here would be a third copy that can disagree with the other two.
    What the timeline contributes is *when*: `TicketEvent.created_at` is the fact the other
    two do not carry, and it is the one §25's realtime event exists to announce.
    """
    event = TicketEvent(
        organization_id=organization_id,
        ticket_id=ticket.id,
        event_type=TicketEventType.AI_ANALYSIS_COMPLETED,
        actor_user_id=None,
        extra_data={"operations": [str(analysis.operation) for analysis in completed]},
    )
    session.add(event)
    return event


def _elapsed_ms(started: float) -> int:
    """Milliseconds since a `time.perf_counter` reading.

    `perf_counter` and not `time.time` for the reason `ai_service._run` gives: this measures
    a duration rather than naming an instant, and a wall clock adjusted by NTP mid-call
    would report a negative one.
    """
    return int((time.perf_counter() - started) * 1000)
