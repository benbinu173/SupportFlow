"""The analysis worker's queries — module-level functions, because a task has no caller.

**Why this module exists rather than a repository class.** Every other read in this
application is a method on a `TenantScopedRepository`, which cannot be constructed without a
`TenantContext`. `app/workers/ai_tasks.py` has no request and no authenticated user, and it
refuses to invent one for the reason `sla_repository.py` gives at length: `role` decides
`permissions`, and a fabricated role is a fabricated authority. Phase U answers that with
`WorkerContext` — a type that carries a tenant and no authority — but `WorkerContext` still
does not open a `TenantScopedRepository`, because those exist to apply a *row* scope derived
from a role, and there is no row scope that means "the background job".

**They live together, in one file**, following `sla_repository.py`: the value of "a function
rather than a method" is that the set of context-free queries stays small enough to count,
and a reader auditing tenant isolation has one file to read. There is no class here and there
should not be one — a class implies state, and the only state any of these has is the session
they were handed.

**Whether a function takes a `Ticket` or an `organization_id` says which side calls it.**
`load_ticket`, `load_analyses` and `load_conversation` are called by
`app/workers/ai_tasks.py` alone, and take `organization_id` as an explicit argument that no
request ever supplies. `latest_by_operation`, `latest_completed_summary` and
`conversation_watermark` *are* reached from a request — `GET /tickets/{id}/ai/analyses` and
`POST /tickets/{id}/ai/summarize` — and they therefore take a **`Ticket` instance rather
than a ticket id**. That is the whole of their access control: on the request path a `Ticket`
can only be obtained through
`require_visible_ticket`, which has already resolved the caller's row scope and returns 404
for a ticket in another tenant. A function that took an id and an `organization_id` would be
callable by a route that had done neither, and the mistake would look like a correct line of
code. It reads the tenant and the ticket from the object it was handed, so there is nothing
left for a caller to get wrong.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_analysis import AIAnalysis
from app.models.enums import AIOperation, ProcessingStatus, SenderType
from app.models.message import Message
from app.models.ticket import Ticket


async def load_ticket(
    session: AsyncSession, organization_id: uuid.UUID, ticket_id: uuid.UUID
) -> Ticket | None:
    """One ticket, if it belongs to this organization.

    `None` rather than an exception for a missing or foreign ticket: both mean the same thing
    to the task — the row it was queued for is not there to analyze — and the task's job is to
    log it and stop, not to raise into a broker where a redelivery would fail identically. The
    `organization_id` predicate is what makes a ticket id from another tenant
    indistinguishable from a ticket id that does not exist.

    Served by the primary key, with the tenant check applied on the row it returns; a ticket
    id is a uuid and an index on it is the only plan worth having.
    """
    result = await session.execute(
        select(Ticket).where(Ticket.id == ticket_id, Ticket.organization_id == organization_id)
    )
    return result.scalar_one_or_none()


async def load_analyses(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    analysis_ids: Sequence[uuid.UUID],
) -> Sequence[AIAnalysis]:
    """The analysis rows a task was queued for, by id, in operation order.

    One query for the whole batch rather than one per id: a ticket's analysis is two rows
    today, and a loop of point lookups is the shape that stops being fine quietly. **Phase V
    did not make it more, and the sentence that said it would is corrected rather than left
    standing** — summarization arrived as a *second entry point over this same worker* rather
    than as a third member of `ANALYSIS_OPERATIONS`, so a summary row is loaded here when a
    summary task was queued and never mixed with an analyse row in one delivery.

    **`ORDER BY operation`, which is the declaration order of the enum** — classify before
    sentiment, and whatever Phase V adds after those. Without it the rows come back in
    whatever order the scan produced, which is stable in practice and not a property
    anything should rest on: the task runs the operations in the order it is handed them,
    so an unordered read makes its log lines, its ledger rows, and the order a scripted
    provider is asked alternate between two equally correct runs. This is the same reading
    `latest_by_operation` takes with the same column, and the order is meaningful for the
    same reason — `request_analysis` writes the rows in `ANALYSIS_OPERATIONS` order, so
    this is the order they were created in.

    Empty `analysis_ids` short-circuits — `IN ()` is not valid SQL — which is also the honest
    answer for a task that was somehow queued for nothing.
    """
    if not analysis_ids:
        return []

    result = await session.execute(
        select(AIAnalysis)
        .where(
            AIAnalysis.id.in_(analysis_ids),
            AIAnalysis.organization_id == organization_id,
        )
        .order_by(AIAnalysis.operation)
    )
    return result.scalars().all()


async def in_flight_analyses(session: AsyncSession, *, ticket: Ticket) -> Sequence[AIAnalysis]:
    """`ticket`'s analyses that have been queued and not yet finished.

    Served by the partial index `ix_ai_analyses_pending`, whose predicate is exactly this
    status pair — the index's comment calls it *"retry sweep and queue monitoring"*, and
    this is the second reader of it.

    Read by `request_analysis` before it queues anything, which is §16's *"avoid duplicate
    processing where possible"* applied to the one way a duplicate is easy to ask for: a
    user pressing "analyze" twice. Without this, two clicks are four queued operations and
    four provider calls for a question that is already in flight, and §53 names repeated AI
    calls as waste. The guard is here rather than in the worker because the waste is
    avoided by not queueing, not by refusing to run.

    Takes the `Ticket` and not an id, like `latest_by_operation` and for the same reason —
    see the module docstring.
    """
    result = await session.execute(
        select(AIAnalysis).where(
            AIAnalysis.ticket_id == ticket.id,
            AIAnalysis.status.in_((ProcessingStatus.PENDING, ProcessingStatus.PROCESSING)),
        )
    )
    return result.scalars().all()


async def latest_by_operation(session: AsyncSession, *, ticket: Ticket) -> Sequence[AIAnalysis]:
    """The most recent analysis of `ticket` for each operation it has been analyzed by.

    The ticket detail screen reads exactly this, per
    `ix_ai_analyses_ticket_operation_created`'s own comment, and that index —
    `(ticket_id, operation, created_at)` — is the plan for this query down to the column
    order.

    **`DISTINCT ON (operation)` and not a window function or a Python fold.** The question is
    "the newest row per operation", which is what `DISTINCT ON` answers in one pass; the
    alternatives are a `row_number()` subquery that returns the same rows through more
    machinery, or fetching every analysis a ticket has ever had and discarding all but the
    newest of each in the application. `ORDER BY` has to lead with the `DISTINCT ON` column,
    which is why the ordering is `operation` first and `created_at` second rather than the
    other way round — so the rows come back grouped by operation, in the enum's declaration
    order, which is also the order a route wants to serve them in.

    Superseded rows are not deleted, and this is the query that makes that invisible rather
    than wasteful: §6 keeps the model's earlier answers *"for comparison and for
    demonstrating AI accuracy over time"*, and a regeneration adds a row rather than
    overwriting one. Latest-per-operation is the reading, not the storage.

    Takes the `Ticket` and not an id — see the module docstring. The tenant and the ticket
    both come from the object, so a caller cannot name either.
    """
    result = await session.execute(
        select(AIAnalysis)
        .where(AIAnalysis.ticket_id == ticket.id)
        .distinct(AIAnalysis.operation)
        .order_by(AIAnalysis.operation, AIAnalysis.created_at.desc())
    )
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Summarization — §20's conversation, and whether the stored summary is current
# ---------------------------------------------------------------------------

#: The senders a summary is made from. **One definition with two readers** — the watermark
#: below and the prompt `app/services/ai_analysis_service.py` builds in the worker — for the
#: reason `app/models/message.py` gives about `MESSAGE_FTS_EXPRESSION`: two spellings of "the
#: conversation" is how the freshness check and the model stop describing the same thing, and
#: the failure is the bad direction — a summary served as current that never saw the message
#: which made it stale.
#:
#: Customer and agent only. A `SYSTEM` row is a status change rather than anything anybody
#: said, and an `AI_DRAFT` is unsent — §21's *"AI must NEVER automatically send a customer-
#: facing response"* made a data question: a draft is not something a person said, so it is
#: not something a summary of what people said should contain.
_CONVERSATION_SENDERS: tuple[SenderType, ...] = (SenderType.CUSTOMER, SenderType.AGENT)


def _conversation() -> ColumnElement[bool]:
    """The predicate every conversation read shares. See `_CONVERSATION_SENDERS`."""
    return Message.sender_type.in_(_CONVERSATION_SENDERS)


@dataclass(frozen=True, slots=True)
class ConversationWatermark:
    """What a summary reads, reduced to what the freshness check needs.

    `count` answers "is there a conversation at all"; `newest` answers "has it moved since
    the summary was made". One aggregate produces both, and both are computed over exactly
    the messages `load_conversation` returns — the property that keeps the check and the
    prompt from disagreeing about what changed.
    """

    count: int
    newest: datetime | None


async def latest_completed_summary(session: AsyncSession, *, ticket: Ticket) -> AIAnalysis | None:
    """`ticket`'s most recent **completed** summary, or `None` if it has never had one.

    Completed and not merely newest: a summary that *failed* is not a summary, and treating
    one as the current answer would mean a failure quietly disabled the cache — §20's "avoid
    regenerating" defeated by the one event that most deserves a retry. A `pending` or
    `processing` row is not this function's business either; `in_flight_analyses` is what
    answers "is work already on its way".

    Served by `ix_ai_analyses_ticket_operation_created`, `(ticket_id, operation, created_at)`,
    which is this query down to the column order — the same index `latest_by_operation`
    reads, narrowed by one equality.

    Takes the `Ticket` and not an id — see the module docstring.
    """
    result = await session.execute(
        select(AIAnalysis)
        .where(
            AIAnalysis.ticket_id == ticket.id,
            AIAnalysis.operation == AIOperation.SUMMARIZE,
            AIAnalysis.status == ProcessingStatus.COMPLETED,
        )
        .order_by(AIAnalysis.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def conversation_watermark(session: AsyncSession, *, ticket: Ticket) -> ConversationWatermark:
    """How much conversation there is, and when it last moved.

    `MAX(created_at)` and not `MAX(updated_at)`: a message is appended and never edited, so
    `created_at` is the instant it joined the conversation and `updated_at` would answer a
    question nobody asked. `count` rides along in the same aggregate because the caller needs
    both and a second query to learn a number the first one already had is the shape this
    module exists to avoid.

    **The comparison it feeds, and the direction its error runs.** The caller compares
    `newest` against the summary row's own `created_at`; both columns are written by the
    database (`server_default=func.now()`), so no application-versus-database clock skew
    enters into it. A summary row is written *before* the task that fills it runs, so a
    message arriving in that window makes a summary that did in fact cover it look stale.
    That costs one regeneration and never serves a summary that missed a message — the
    conservative column is the correct one, and `completed_at` would have meant comparing an
    application clock against a database one.

    The organization filter is applied here as well as the ticket, which
    `latest_by_operation` does not do: the ticket carries the tenant, a message carrying a
    different one is a bug rather than a case to tolerate, and `MessageRepository`'s own
    docstring is that the organization filter applies independently of any upstream
    resolution.
    """
    result = await session.execute(
        select(func.count(), func.max(Message.created_at)).where(
            Message.ticket_id == ticket.id,
            Message.organization_id == ticket.organization_id,
            _conversation(),
        )
    )
    count, newest = result.one()
    return ConversationWatermark(count=int(count), newest=newest)


async def load_conversation(
    session: AsyncSession, organization_id: uuid.UUID, *, ticket_id: uuid.UUID
) -> Sequence[Message]:
    """One ticket's conversation, oldest first, for the worker to summarize.

    The same eligibility rule as `conversation_watermark` and the same ordering as
    `MessageRepository.list_for_ticket` — `created_at` with `id` as a tiebreaker, so two
    messages written in one transaction still have a deterministic order — because a
    conversation is read forwards and a summary that reordered the turns would be summarizing
    a different conversation. Served by `ix_messages_ticket_created`.

    **No limit, deliberately.** A conversation longer than the model's context fails visibly:
    the provider refuses the prompt, the row is marked `failed`, and `error_message` says so
    on the ticket. Truncating to fit would produce a summary that silently omitted the
    beginning and was indistinguishable from one that had read it all — a wrong answer wearing
    the same shape as a right one, which §60's ban on fake AI results is about. A character
    budget that keeps the newest turns and says what it dropped is the honest version of a
    limit, and it is not in this phase.

    Worker path, so `organization_id` is explicit and a request never supplies one — see the
    module docstring.
    """
    result = await session.execute(
        select(Message)
        .where(
            Message.ticket_id == ticket_id,
            Message.organization_id == organization_id,
            _conversation(),
        )
        .order_by(Message.created_at, Message.id)
    )
    return result.scalars().all()
