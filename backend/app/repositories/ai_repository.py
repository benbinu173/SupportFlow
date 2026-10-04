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

**Two of the three are worker-only and the third is not, and the difference is structural.**
`load_ticket` and `load_analyses` are called by `app/workers/ai_tasks.py` alone, and take
`organization_id` as an explicit argument that no request ever supplies.
`latest_by_operation` *is* reached from a request — `GET /tickets/{id}/ai/analyses` — and it
therefore takes a **`Ticket` instance rather than a ticket id**. That is the whole of its
access control: on the request path a `Ticket` can only be obtained through
`require_visible_ticket`, which has already resolved the caller's row scope and returns 404
for a ticket in another tenant. A function that took an id and an `organization_id` would be
callable by a route that had done neither, and the mistake would look like a correct line of
code. It reads the tenant and the ticket from the object it was handed, so there is nothing
left for a caller to get wrong.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_analysis import AIAnalysis
from app.models.enums import ProcessingStatus
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
    today and will be more when Phase V adds summarization, and a loop of point lookups is
    the shape that stops being fine quietly.

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
