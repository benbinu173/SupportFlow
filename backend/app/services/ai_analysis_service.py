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

**Summarization is a second entry point over this same worker, not a third operation.**
§20's summary is not something "analyze this ticket" performs — §36 gives it its own route,
and the thing it reads is the conversation rather than the ticket's two text fields. So
`ANALYSIS_OPERATIONS` stays two members, and `request_summary` writes one `SUMMARIZE` row
where `request_analysis` writes two. Everything below the entry point is shared: the same
`run_analysis`, the same task, the same terminal-status skip, the same failure containment,
the same ledger. A second worker path would be a second place for §16's duplicate guard to
be missing from.

**§21's suggested reply is a third, and it is the one that writes a message.** It is a second
entry point in exactly the sense above — its own route, one operation, and no place in
`ANALYSIS_OPERATIONS` — and it differs from both in what it leaves behind: the `SUGGEST_RESPONSE`
row holds the model's draft, and an `ai_draft` `Message` puts it into the thread, because §41's
distinction between a draft and a reply is one `SenderType` already makes. It shares the
summary's property of writing no column on the ticket, and it deliberately does **not** share
the summary's cache: §41 lists regenerate as an action a person may take, so a second request
is a request for a *different* draft rather than a repeat of one question.

**§20's "avoid regenerating after every tiny message if unnecessary" is a comparison between
two stored timestamps, not a cache.** When a summary is asked for and no message has arrived
since the last one was made, the stored summary *is* the answer: `request_summary` returns
it, writes no `ai_analyses` row, queues nothing, and records the call it did not make with
`ai_service.record_cache_hit`. There is no Redis entry and no TTL, because the thing being
reused is the row §20's own first sentence asks for — *"store the latest summary"* — and a
second store of one fact is a second thing to keep consistent. `was_cached` on the ledger row
is what makes the saving countable rather than estimated.

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
from app.core.exceptions import AIServiceError, ValidationError
from app.core.tenancy import RequestOrigin, TenantContext, WorkerContext
from app.models.ai_analysis import AIAnalysis
from app.models.enums import (
    AIOperation,
    AuditAction,
    ProcessingStatus,
    SenderType,
    TicketEventType,
)
from app.models.message import Message
from app.models.notification import Notification
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.repositories import ai_repository
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)
from app.services import ai_service, audit_service, knowledge_service, notification_service
from app.websocket import manager as realtime

logger = structlog.get_logger(__name__)

#: The operations one "analyze this ticket" request performs. §51's list, minus the parts
#: that are other requests: **summarization is §20 and it deliberately did not join this
#: tuple** — Phase V gave it `request_summary` and `/ai/summarize`, and the conversation that
#: reads is not the ticket text these two read. **Suggested replies are §21, and Phase W gave
#: them `request_suggested_response` and `/ai/suggest-response` for the same reason**: the
#: material is the ticket *and* the conversation, and the answer is a draft rather than a field
#: on the row. **§23's knowledge answer is not here either, and its reason is the strongest of
#: the three**: it is not an operation *on a ticket* at all — it is a question someone asks, and
#: Phase X answers it on a route rather than on an `ai_analyses` row. The tuple is ordered
#: because the rows are written in this order and a client reading them back sees it.
ANALYSIS_OPERATIONS: tuple[AIOperation, ...] = (
    AIOperation.CLASSIFY,
    AIOperation.SENTIMENT,
)

#: The instruction each operation is called with. A dict rather than a branch, so
#: `_request_for` cannot be reached with an operation that has no ticket-shaped instruction —
#: it raises a `KeyError` naming the operation, which is the failure a reader would want.
#: **`SUMMARIZE` is absent on purpose**: its instruction is
#: `prompts.CONVERSATION_SUMMARY_INSTRUCTION` and its content is a conversation rather than
#: `prompts.ticket_content`, so `_summary_request` builds that call and this dict must not
#: pretend to.
_INSTRUCTIONS: dict[AIOperation, str] = {
    AIOperation.CLASSIFY: prompts.CLASSIFICATION_INSTRUCTION,
    AIOperation.SENTIMENT: prompts.SENTIMENT_INSTRUCTION,
}

#: What the fenced block is called in the prompt. **The caller's own words**, per
#: `app/ai/prompts.py`'s rule: the label is prose the model reads as instruction, so it must
#: not be anything a customer wrote. "The customer's ticket" describes the block; the ticket's
#: subject would be part of it.
_CONTENT_LABEL = "the customer's support ticket"

#: The same, for §20's conversation block. A label rather than the ticket's subject for the
#: reason above, and separate from `_CONTENT_LABEL` because the two describe different things:
#: a summary is asked about a thread of replies, not about the description that opened it.
_CONVERSATION_LABEL = "the support conversation so far"

#: The same, for §21's block — which is the two above in one user message, plus §22's retrieved
#: passages when there are any, so the label names what the block can contain. Still the caller's
#: own words and never anything a customer wrote, which is the rule `app/ai/prompts.py` states
#: and the reason it is a constant here rather than a string built from the ticket's subject.
#: "any passages" rather than "the passages" because the overwhelming majority of drafts have
#: none — a tenant that never opened the knowledge base, or a question nothing cleared the
#: threshold for — and the model should not be told to expect a block that is not there.
_DRAFT_LABEL = (
    "the customer's support ticket, the conversation so far, and any knowledge base passages "
    "retrieved for it"
)

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

    Built by `_reduced` from the provider's `AIResult`, and it exists because the three
    schemas have no common supertype beyond `BaseModel` — the row is written from this
    rather than from a union the type checker cannot narrow.

    `confidence` is optional because §20's summary has none. The other two schemas carry one;
    `ai_analyses.confidence` is nullable with a range check, so `None` is a value the column
    already accepts rather than a gap this dataclass is working around.
    """

    confidence: float | None
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


async def request_summary(
    session: AsyncSession,
    context: TenantContext,
    ticket: Ticket,
    *,
    origin: RequestOrigin | None = None,
) -> AIAnalysis:
    """Summarize this ticket's conversation, or return the summary it already has.
    **Commits when it does work.**

    §20 in four sentences, and this function answers three of them. *"Store the latest
    summary"* is an `ai_analyses` row with `operation = SUMMARIZE` — which
    `latest_by_operation` already reduces to one per operation and
    `GET /tickets/{id}/ai/analyses` already serves, so there is no summary column and no
    second table. *"Allow regeneration when the conversation changes significantly"* is a
    second call to this function, which writes a new row and leaves the old one for §6's
    comparison. *"Avoid regenerating after every tiny message if unnecessary"* is the check
    below.

    **Three outcomes, in the order a caller cares about them.**

    1. **Work is already on its way.** An in-flight `SUMMARIZE` row is returned rather than a
       second one queued — §16's *"avoid duplicate processing"*, the guard `request_analysis`
       applies, and for the same reason: two clicks would be two provider calls for a
       question that is already in flight.
    2. **The stored summary is current.** No message has arrived since it was made, so it
       *is* the answer. The audit row and a `was_cached=True` ledger row are written, the
       tenant's analytics are invalidated because a ledger row moved them, and the existing
       row comes back. **No `ai_analyses` row is created and nothing is queued.**
    3. **The conversation moved, or has never been summarized.** A `PENDING` row is created,
       stamped with the configured provider and model, and queued.

    **The freshness comparison is between two database timestamps** — the newest eligible
    message's `created_at` and the stored summary row's own `created_at`. See
    `ai_repository.conversation_watermark` for why that column rather than `completed_at`,
    and for the direction its error is allowed to run in.

    **An empty conversation is refused.** §20 is about *"long ticket conversations"*, and a
    ticket whose description has not yet been followed by a reply has nothing to summarize:
    the model would be handed an empty block and asked to summarize it. `ValidationError`
    says so rather than spending a call to find out — the same explicit refusal
    `create_ticket` makes when a portal caller names somebody else's `customer_id`.

    Takes the `Ticket` and not an id, like `request_analysis` and for its reason: both
    callers have one, resolving it again would be a second query for the route and a wasted
    one for `create_ticket`, and this function would gain an authorization decision its
    callers have already made.
    """
    in_flight = [
        queued
        for queued in await ai_repository.in_flight_analyses(session, ticket=ticket)
        if queued.operation is AIOperation.SUMMARIZE
    ]
    if in_flight:
        logger.info(
            "ai_summary_already_queued",
            ticket_id=str(ticket.id),
            organization_id=str(context.organization_id),
        )
        return in_flight[0]

    watermark = await ai_repository.conversation_watermark(session, ticket=ticket)
    if watermark.count == 0:
        raise ValidationError("This ticket has no conversation to summarize.")

    stored = await ai_repository.latest_completed_summary(session, ticket=ticket)
    # `watermark.newest` cannot be `None` past the check above — a count of nought is the
    # only way an aggregate over no rows reads — but the comparison is written to tolerate it
    # rather than asserting what the aggregate already guarantees.
    if stored is not None and (watermark.newest is None or watermark.newest <= stored.created_at):
        audit_service.record_for(
            session,
            context,
            AuditAction.AI_ANALYSIS_REQUESTED,
            target_type="ticket",
            target_id=ticket.id,
            metadata={"operations": [str(AIOperation.SUMMARIZE)], "cached": True},
            origin=origin,
        )
        # The row recording the call nobody made — see `ai_service.record_cache_hit`. The
        # provider and model are the stored summary's and not the current configuration's:
        # they name the model whose answer is being reused, which is the fact a ledger row
        # is supposed to carry.
        ai_service.record_cache_hit(
            session,
            context,
            operation=AIOperation.SUMMARIZE,
            provider=stored.provider,
            model=stored.model,
            ticket_id=ticket.id,
        )
        await session.commit()
        # A ledger row moved `/analytics/overview`'s totals, so the tenant's cached entries
        # move with it — the same post-commit position every other writer invalidates from.
        await cache.invalidate(context.organization_id)
        logger.info(
            "ai_summary_cached",
            ticket_id=str(ticket.id),
            organization_id=str(context.organization_id),
            analysis_id=str(stored.id),
        )
        return stored

    settings = get_settings()
    created = AIAnalysis(
        organization_id=context.organization_id,
        ticket_id=ticket.id,
        operation=AIOperation.SUMMARIZE,
        status=ProcessingStatus.PENDING,
        # Stamped at queue time for the reason `request_analysis` gives: config changes, and a
        # historical row has to keep naming the model it asked.
        provider=settings.AI_PROVIDER,
        model=settings.AI_MODEL,
    )
    session.add(created)
    audit_service.record_for(
        session,
        context,
        AuditAction.AI_ANALYSIS_REQUESTED,
        target_type="ticket",
        target_id=ticket.id,
        metadata={"operations": [str(AIOperation.SUMMARIZE)], "cached": False},
        origin=origin,
    )
    await session.commit()

    enqueue_analysis(ticket.id, context.organization_id, analysis_ids=[created.id])

    logger.info(
        "ai_summary_queued",
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        actor_id=str(context.user_id),
    )
    return created


async def request_suggested_response(
    session: AsyncSession,
    context: TenantContext,
    ticket: Ticket,
    *,
    origin: RequestOrigin | None = None,
) -> AIAnalysis:
    """Queue a draft reply for this ticket. **Commits.**

    §21 in one row and §41's four verbs in two of them. *"generated"* is this: a
    `SUGGEST_RESPONSE` row, `pending` and then `completed`, holding the model's own output in
    `result` like every other operation. *"regenerated"* is a second call to this function,
    and the trail says which of the two happened — the audit action is
    `AI_RESPONSE_REGENERATED` when a completed `SUGGEST_RESPONSE` row already exists for the
    ticket and `AI_ANALYSIS_REQUESTED` otherwise. **The decision is read from stored state
    here**, exactly as `request_summary` decides cached-versus-queued, which is why §41 needs
    neither a second route nor a `?force=`: asking again *is* regenerating.

    **No cache, and the difference from `request_summary` above is the point.** §20 reuses a
    summary when nothing has changed because one conversation has one right answer. A draft
    has many, and §41 lists regenerate as something a person may do on purpose — so every
    request that is not already in flight makes a real call and writes a real row. A
    `was_cached` entry for this operation would be a cache defeating the feature it sits in.
    `app/repositories/ai_repository.py` therefore has no `latest_completed_suggestion` and
    needs none: the only question asked of the past here is *"has there ever been one"*.

    **In flight is not queued twice.** §16's duplicate guard, applied as in both functions
    above and for their reason: two clicks must not be two provider calls for a draft that is
    already on its way.

    **`edited` and `accepted` are the other two verbs and neither is here.** Both happen when
    a person sends a draft rather than when one is asked for, so they live on
    `message_service.post_reply` — `AuditAction.AI_RESPONSE_ACCEPTED`, carrying §34's
    before/after, which is the mechanism those two verbs were written for.

    Takes the `Ticket` and not an id, like its two siblings and for their reason: both callers
    have one, resolving it again would be a second query for the route, and this function
    would gain an authorization decision its callers have already made.
    """
    in_flight = [
        queued
        for queued in await ai_repository.in_flight_analyses(session, ticket=ticket)
        if queued.operation is AIOperation.SUGGEST_RESPONSE
    ]
    if in_flight:
        logger.info(
            "ai_suggestion_already_queued",
            ticket_id=str(ticket.id),
            organization_id=str(context.organization_id),
        )
        return in_flight[0]

    regenerating = await ai_repository.has_completed_suggestion(session, ticket=ticket)

    settings = get_settings()
    created = AIAnalysis(
        organization_id=context.organization_id,
        ticket_id=ticket.id,
        operation=AIOperation.SUGGEST_RESPONSE,
        status=ProcessingStatus.PENDING,
        # Stamped at queue time for the reason `request_analysis` gives: config changes, and a
        # historical row has to keep naming the model it asked.
        provider=settings.AI_PROVIDER,
        model=settings.AI_MODEL,
    )
    session.add(created)
    audit_service.record_for(
        session,
        context,
        AuditAction.AI_RESPONSE_REGENERATED if regenerating else AuditAction.AI_ANALYSIS_REQUESTED,
        target_type="ticket",
        target_id=ticket.id,
        metadata={"operations": [str(AIOperation.SUGGEST_RESPONSE)]},
        origin=origin,
    )
    await session.commit()

    enqueue_analysis(ticket.id, context.organization_id, analysis_ids=[created.id])

    logger.info(
        "ai_suggestion_queued",
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        actor_id=str(context.user_id),
        regenerated=regenerating,
    )
    return created


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

    **A run that changed no ticket column announces without alerting.** A summary is the one
    operation that finishes without writing a ticket field, and an alert to the assignee about
    a summary they asked for is an interruption about something they are already reading. The
    timeline entry and the socket event still happen: the entry is the only record of *when*,
    and the event is what a client polling between asking and the answer arriving is waiting
    for.
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
        # §18's last step, and it rings only for a run that changed the ticket. A summary
        # changed nothing — §20 stores one and writes no column — and the person who asked for
        # it is the person reading it, so the alert would announce to somebody what they are
        # already looking at. The timeline entry and the socket event still happen; see the
        # docstring.
        if any(analysis.operation in ANALYSIS_OPERATIONS for analysis in completed_this_run):
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
    """Make one operation's call, and copy what belongs on the ticket.

    Returns `None` for an operation this build has no implementation for, which the caller
    records as a failure. That case is unreachable while the operations a route can queue are
    exactly the four implemented below — and reachable the moment they are not, because a
    task is handed ids over a broker and a deployment mid-rollout can deliver a row from a
    version that knew an operation this one does not. **Phase X implemented the last of the five
    operations and did not add a branch here**: §23's `KNOWLEDGE_ANSWER` is a question a person
    asks, answered by `knowledge_service.answer` on its own route, so a row carrying it is a row
    that should not exist — and this is what says so on the ticket where a person will see it,
    rather than dying in a worker log.

    **The ticket is written here, per operation, and not from the row afterwards.** The
    mapping from a validated model to the columns it feeds is the one place this module and
    `app/schemas/ai.py` have to agree, and writing it as typed branches means a field renamed
    on one side is a mypy error rather than a column that silently stops being written.
    **§20's summary and §21's draft are the two branches that write nothing onto the ticket**:
    a summary is stored rather than applied, and a draft is stored as a message rather than
    sent — the row's own `result` is where each lives.
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

    if analysis.operation is AIOperation.SUMMARIZE:
        conversation = await ai_repository.load_conversation(
            session, context.organization_id, ticket_id=ticket.id
        )
        summarized = await ai_service.summarize_conversation(
            session, context, _summary_request(conversation), ticket_id=ticket.id
        )
        # **Nothing is written onto the ticket**, and that is the whole of this branch's
        # difference: §20 stores a summary rather than applying one, and the row's `result`
        # is where it lives. It is also what makes a summary-only run the one completion that
        # announces without notifying — see `run_analysis`.
        return _reduced(summarized)

    if analysis.operation is AIOperation.SUGGEST_RESPONSE:
        conversation = await ai_repository.load_conversation(
            session, context.organization_id, ticket_id=ticket.id
        )
        # §21's "relevant knowledge" step, and it is **fail-open**: a knowledge base that cannot
        # be reached must not stop a draft being written. The retrieval is one embedding call,
        # and an embedding provider having a bad afternoon is not a reason to refuse to help an
        # agent reply — a draft that is merely less grounded is worth more than no draft. The
        # failure is logged with its type and the run carries on with the two blocks that already
        # exist, which is why a retrieval outage is invisible to an agent except as a reply that
        # cites nothing.
        passages = await _knowledge_for_draft(session, context, ticket)
        drafted = await ai_service.generate_response(
            session,
            context,
            _draft_request(ticket, conversation, passages),
            ticket_id=ticket.id,
        )
        # §41's distinction as a row rather than as a flag. The model's draft becomes a
        # `Message` with `sender_type = AI_DRAFT`, which `ai_draft_is_internal` (Phase D) makes
        # impossible for a customer to read and `MessageRepository.list_for_ticket` already
        # returns to staff — so §21's "explicit UI distinction between AI generated draft and
        # human-authored response" is a field the message read already carries.
        #
        # Built and added directly rather than through `MessageRepository`: that repository is
        # constructed from a `TenantContext`, and this is the worker path with a
        # `WorkerContext` and no authority to widen. `_record_completion` below adds a
        # `TicketEvent` the same way, for the same reason.
        #
        # **Nothing is written onto the ticket**, which is what makes a suggestion-only run
        # announce without alerting. `sender_user_id` is NULL because the model is not a user
        # — who asked for the draft is on the `ai_analyses` row and in the audit trail.
        session.add(
            Message(
                organization_id=context.organization_id,
                ticket_id=ticket.id,
                sender_type=SenderType.AI_DRAFT,
                sender_user_id=None,
                body=drafted.value.body,
                is_internal=True,
            )
        )
        return _reduced(drafted)

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


def _summary_request(conversation: Sequence[Message]) -> AIRequest:
    """The §20 call for one conversation.

    Its own function rather than a branch inside `_request_for`, because the two are handed
    different things: that one takes a `Ticket` and reads its two text fields, and this one
    takes the messages the worker loaded. `max_tokens` comes from settings in both, for the
    reason `AIRequest`'s docstring gives — it has one home.

    The fence is still the provider's. `prompts.conversation_content` renders the turns and
    stops there, which is what keeps one implementation of the mechanism rather than two.
    """
    return AIRequest(
        instruction=prompts.CONVERSATION_SUMMARY_INSTRUCTION,
        content=prompts.conversation_content(conversation),
        content_label=_CONVERSATION_LABEL,
        max_tokens=get_settings().AI_MAX_TOKENS,
    )


def _draft_request(
    ticket: Ticket, conversation: Sequence[Message], knowledge: Sequence[str]
) -> AIRequest:
    """The §21 call for one ticket, its conversation, and any passages retrieved for it.

    Its own function for `_summary_request`'s reason: `_request_for` takes a `Ticket` and
    reads its two text fields, and this one is handed all three. `max_tokens` comes from
    settings in all three, for the reason `AIRequest`'s docstring gives — it has one home.

    `knowledge` is required rather than defaulted so that every caller has decided what to
    retrieve, and the decision to retrieve *nothing* is then a value (`[]`) rather than an
    omission — the fail-open path in `_execute` passes an empty sequence on purpose, and a
    default here would make that look the same as forgetting to pass it.

    The fence is still the provider's. `prompts.draft_content` assembles the ticket's words, the
    conversation's, and the passages and stops there, which is what keeps one implementation of
    the mechanism rather than three.
    """
    return AIRequest(
        instruction=prompts.SUGGESTED_REPLY_INSTRUCTION,
        content=prompts.draft_content(ticket, conversation, knowledge),
        content_label=_DRAFT_LABEL,
        max_tokens=get_settings().AI_MAX_TOKENS,
    )


async def _knowledge_for_draft(
    session: AsyncSession, context: WorkerContext, ticket: Ticket
) -> list[str]:
    """§22's passages for a draft, or `[]` if the knowledge base cannot be reached.

    **Fail-open, and the containment is the point.** `knowledge_service.retrieve` makes one
    embedding call, and that call can fail; an agent waiting for a suggested reply is not
    served by a 503 from a feature they did not ask for. `AIServiceError` — which is what
    `ai_service` raises when a provider is unreachable or untrustworthy — is logged with its
    type and turned into an empty list, so the draft is built from the ticket and the
    conversation alone. **A narrower catch than `run_analysis`'s per-operation one on purpose**:
    a database error or a bug in the retrieval query still propagates, because swallowing those
    would hide a real fault behind a reply that merely cites nothing.

    The query is the ticket's own words, subject and description, which is the material §21
    is about. Sentiment is not in it: retrieval is a similarity search over documents, and
    "frustrated" is not a phrase a policy page contains.

    **A tenant with no published chunks never reaches the provider** — `retrieve` asks
    `has_published_chunks` first — so the common case costs one indexed read rather than an
    embedding, and §21's behaviour for such a tenant is exactly what it was before Phase X.
    """
    query = f"{ticket.subject}\n\n{ticket.description}"
    try:
        matches = await knowledge_service.retrieve(session, context, query, ticket_id=ticket.id)
    except AIServiceError as exc:
        logger.warning(
            "knowledge_draft_retrieval_failed",
            ticket_id=str(ticket.id),
            error_type=type(exc).__name__,
        )
        return []
    return [match.content for match in matches]


def _reduced(
    result: AIResult[Classification]
    | AIResult[SentimentResult]
    | AIResult[ConversationSummary]
    | AIResult[SuggestedReply],
) -> _Outcome:
    """One provider result reduced to what the analysis row stores.

    **The model's own dump, re-serialized, and the token counts untouched.** The payload is
    `mode="json"` so the enums become their wire values before they reach JSONB — a
    `StrEnum` would serialize correctly through `json.dumps` anyway, and the mode makes that
    a property of this function rather than of every future field.

    **`confidence` is read only from the schemas that have one.** §18's classification and
    §19's sentiment each carry a confidence and §20's summary and §21's draft deliberately do
    not — a summary is a description rather than a judgement, and a draft is edited rather than
    scored. The column's own check constraint already allows NULL, and naming the two types is
    what keeps the absence a property of the schemas rather than of a `getattr` that would hand
    back `None` for a field somebody misspelled.
    """
    value = result.value
    confidence = value.confidence if isinstance(value, (Classification, SentimentResult)) else None
    return _Outcome(
        confidence=confidence,
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
