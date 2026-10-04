"""AI endpoints — asking for an analysis, and reading what came back.

Mounted under `/tickets/{ticket_id}` rather than at an `/ai` root, and for the reason
`app/api/messages.py` gives: an analysis has no independent existence. It is reachable
exactly when its ticket is, the path says so, and there is no `/ai/analyses/{id}` route
that would need its own access rule derived from the ticket's (ADR-015).

**Three routes, one capability, and none of them is `TICKET_VIEW`.** §3's matrix gives
customers no AI access at all, so a ticket read capability here would hand a portal caller
the analysis of their own ticket — including `error_message`, which the column's own comment
says is *"surfaced to staff, never to customers: upstream errors can echo prompt content."*
`AI_REQUEST_ANALYSIS` is the capability that gates asking and reading, which is the same
reading `GET /customers/{id}` → `CUSTOMER_LIST` takes: one capability for the operation,
rather than one per verb. Summarizing is a form of asking for an analysis of the ticket, so
§20's route takes the same capability as §18's — `AI_REQUEST_SUGGESTION`,
`AI_QUERY_KNOWLEDGE`, and `AI_VIEW_USAGE` belong to Phases W, X, and the analytics routes
that already exist.

**The route is not where the work happens.** §16's *"The API should not wait unnecessarily
for the LLM"* is satisfied structurally — nothing in this module imports a provider.
`request_analysis` writes the rows and hands a task to a broker, and the model is called by
`app/workers/ai_tasks.py` in another process. The 202 is the honest status for that: the
request was accepted and the result is not in this response.

**Both routes resolve the ticket first.** `require_visible_ticket` applies the caller's row
scope, so a ticket in another tenant is a 404 on both — indistinguishable from one that
does not exist, which is the property `tests/security/test_ai_isolation.py` asserts.
"""

import uuid

from fastapi import APIRouter, Depends, status

from app.api.deps import Context, DbSession, Origin, require_permission
from app.api.rate_limits import limit_ai
from app.core.permissions import Permission
from app.repositories import ai_repository
from app.schemas.analysis import AIAnalysisRead
from app.services import ai_analysis_service, ticket_service

router = APIRouter()


@router.post(
    "/{ticket_id}/ai/analyze",
    response_model=list[AIAnalysisRead],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a ticket for AI analysis",
    dependencies=[
        Depends(require_permission(Permission.AI_REQUEST_ANALYSIS)),
        Depends(limit_ai),
    ],
)
async def analyze_ticket(
    ticket_id: uuid.UUID, context: Context, db: DbSession, origin: Origin
) -> list[AIAnalysisRead]:
    """Queue this ticket's classification and sentiment analysis.

    **202 and not 201**, because the response body is not the result. It is the record of
    what was queued: one row per operation, `pending`, carrying the provider and model that
    will be asked. A client that wants the answer reads
    `GET /tickets/{ticket_id}/ai/analyses`, which is why both routes exist rather than one
    that blocks — an analysis that took ten seconds would hold a request, a connection, and
    a browser waiting on it.

    **Asking twice is not paying twice.** `request_analysis` returns what is already in
    flight rather than queueing a second copy, so the idempotent case — a double-click, a
    retry after a timeout — answers 202 with the rows the first request created. The status
    code does not distinguish the two and should not: from the caller's point of view the
    question "is this ticket being analyzed" has the same answer either way.

    Rate limited per user (§45, `limit_ai`), which is the guard that matters here — this is
    the one route in the API whose abuse is billed rather than suffered.
    """
    ticket = await ticket_service.require_visible_ticket(db, context, ticket_id)
    analyses = await ai_analysis_service.request_analysis(db, context, ticket, origin=origin)
    return [AIAnalysisRead.model_validate(analysis) for analysis in analyses]


@router.post(
    "/{ticket_id}/ai/summarize",
    response_model=AIAnalysisRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Summarize a ticket's conversation",
    dependencies=[
        Depends(require_permission(Permission.AI_REQUEST_ANALYSIS)),
        Depends(limit_ai),
    ],
)
async def summarize_ticket(
    ticket_id: uuid.UUID, context: Context, db: DbSession, origin: Origin
) -> AIAnalysisRead:
    """Summarize this ticket's conversation — §20, and §36's second AI route.

    **One row, not a list.** `analyze_ticket` answers with a list because one request queues
    two operations; this one queues the single operation §20 describes, so the response is
    the one row it is about.

    **`202` covers both outcomes, and the row says which happened.** Work was queued and the
    row is `pending`; or the conversation had not moved since the last summary, in which case
    §20's *"avoid regenerating after every tiny message if unnecessary"* means the stored
    summary *is* the answer, the row comes back `completed`, and no second call is made. This
    is the same reading `analyze_ticket` takes of a double-click: from the caller's point of
    view the question "what is the summary of this conversation" has an answer either way, and
    the status code does not need to distinguish how it was reached.

    **The caller polls, or waits for the socket.** A queued summary is filled by the worker
    like any other analysis, and `GET /tickets/{ticket_id}/ai/analyses` returns it as the
    latest `SUMMARIZE` row alongside the classification and the sentiment — which is where a
    summary is read from, and why §36 needs no `GET` route for one.

    Rate limited per user (§45, `limit_ai`) for `analyze_ticket`'s reason: this is the abuse
    that is billed rather than suffered.
    """
    ticket = await ticket_service.require_visible_ticket(db, context, ticket_id)
    analysis = await ai_analysis_service.request_summary(db, context, ticket, origin=origin)
    return AIAnalysisRead.model_validate(analysis)


@router.get(
    "/{ticket_id}/ai/analyses",
    response_model=list[AIAnalysisRead],
    summary="A ticket's AI analyses",
    dependencies=[Depends(require_permission(Permission.AI_REQUEST_ANALYSIS))],
)
async def list_analyses(
    ticket_id: uuid.UUID, context: Context, db: DbSession
) -> list[AIAnalysisRead]:
    """The most recent analysis of each kind this ticket has had.

    **This is what makes a queued analysis visible.** `AIAnalysis`'s docstring says a row
    exists from the moment work is queued, *"rather than silently absent"* — and a row
    nobody can read is silently absent for all practical purposes. A client polls this
    between raising a ticket and the completion event arriving on the socket, and a failed
    analysis is visible here with its `error_message` rather than only in a worker log.

    **Latest per operation, not every row.** §6 keeps the model's earlier answers for
    comparison, so a regeneration adds a row; serving all of them would bury the current
    answer under its history. `latest_by_operation` takes a `Ticket` rather than an id, so
    the row scope resolved above is the only way this route can name one — see
    `app/repositories/ai_repository.py`.
    """
    ticket = await ticket_service.require_visible_ticket(db, context, ticket_id)
    analyses = await ai_repository.latest_by_operation(db, ticket=ticket)
    return [AIAnalysisRead.model_validate(analysis) for analysis in analyses]
