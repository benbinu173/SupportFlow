"""The analysis task — §18's pipeline, run outside the request that asked for it.

**Why this is a task and not a route.** §16 is explicit that *"the API should not wait
unnecessarily for the LLM"*, and a language model call is the slowest thing this system
does. A ticket is created and the response goes back; the analysis happens in another
process, on its own queue, and reaches the browser through the same realtime channel an SLA
alert uses.

**All of the work is in `ai_analysis_service.run_analysis`, and that is deliberate.** What is
left here is a task decorator, an engine, and an event loop — the same division
`app/workers/sla_tasks.py` takes with `sla_service`, and it is what lets the whole pipeline be
tested against a real database with no broker in the picture
(`tests/integration/test_ai_analysis.py` calls `run_analysis` directly).

**The task is handed references, never a payload.** A ticket id, an organization id, and the
ids of the `ai_analyses` rows to fill — the same argument `send_notification_email` and
`check_organization_sla` make: a snapshot copied into the broker is stale by the time it runs,
and a Redis restart without persistence loses it while the database still shows the work as
queued. Ids are strings because the broker serializes to JSON, so a `uuid.UUID` would arrive
as a string regardless; converting at the boundary keeps the signature honest about what comes
over the wire.

**`worker_context` and not a fabricated `TenantContext`.** This process has no authenticated
user. `WorkerContext` is the type that says so — a tenant and no authority — and
`app/services/ai_service.py` accepts it for the one thing it decides, which is whose ledger
row the spend lands on. Inventing a user id and a role here would be inventing an identity,
which is the class of thing ADR-009 and §4 exist against.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import event_loop
from app.core.config import get_settings
from app.core.tenancy import WorkerContext
from app.services import ai_analysis_service
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

_settings = get_settings()

# No pool, for the reason `email_tasks` and `sla_tasks` give: `event_loop.run` builds a new
# loop per task, and a pooled psycopg connection belongs to the loop that opened it — the
# second task to run would be handed a connection from a loop that has closed. `echo` stays
# off, because a statement log carries query parameters and the parameters here are ticket
# subjects.
_engine = create_async_engine(str(_settings.DATABASE_URL), poolclass=NullPool)

_SessionFactory = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    # Readable after commit: `run_analysis` stages notifications and then commits, and the
    # delivery task is handed their ids. A lazy refresh here would raise in async code.
    expire_on_commit=False,
)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.workers.ai_tasks.analyze_ticket",
)
def analyze_ticket(ticket_id: str, organization_id: str, analysis_ids: list[str]) -> dict[str, int]:
    """Analyze one ticket. Returns counts, never a rendered result.

    The result backend is a debugging aid — the durable record is the `ai_analyses` rows,
    the ticket's own columns, the timeline entry, and the ledger. A task that returned the
    classification would invite a caller to read it from here instead of from the row that
    survives a Redis flush, and the counts answer the only question the backend is good for:
    did anything actually happen.

    **No retry, and none is wanted.** `ai_service._run` is where §17's retry policy lives and
    it has already applied it to every call this task makes; a second loop at this level
    would multiply the two. A redelivery — which `task_acks_late` makes possible — is
    handled inside `run_analysis` by the row status: a task that died after committing its
    results finds nothing left to claim.
    """
    return event_loop.run(
        _analyze(
            ticket_id=uuid.UUID(ticket_id),
            organization_id=uuid.UUID(organization_id),
            analysis_ids=[uuid.UUID(analysis_id) for analysis_id in analysis_ids],
        )
    )


async def _analyze(
    *, ticket_id: uuid.UUID, organization_id: uuid.UUID, analysis_ids: list[uuid.UUID]
) -> dict[str, int]:
    """One session for the whole analysis, opening and closing inside this task's loop.

    `async with` around the session rather than a bare construction: `run_analysis` commits
    several times and publishes afterwards, and a session left open would hold a connection
    until the object was collected — which in a worker processing one analysis per ticket
    arrival is a slow leak rather than an error anybody sees.
    """
    async with _SessionFactory() as session:
        return await ai_analysis_service.run_analysis(
            session,
            WorkerContext(organization_id=organization_id),
            ticket_id=ticket_id,
            analysis_ids=analysis_ids,
        )
