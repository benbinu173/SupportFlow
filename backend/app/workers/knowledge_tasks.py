"""The ingestion task — §22's pipeline, run outside the request that registered a document.

**Why this is a task and not a route.** §22's pipeline is four kind of slow in a row: fetching a
page over the network (optionally six hops of it), reading a PDF, and then one embedding call per
sixty-four chunks. §16's *"the API should not wait unnecessarily"* rules all of it out of a request
that a person is watching, so the document is created `pending`, the response goes back, and the
work happens here, on the knowledge queue.

**All of the work is in `knowledge_service.run_ingestion`, and that is deliberate.** What is left
here is a task decorator, an engine, and an event loop — `ai_tasks.py`'s division exactly, and for
its reason: the whole pipeline is testable against a real database with no broker in the picture
(`tests/integration/test_knowledge_ingestion.py` calls `run_ingestion` directly).

**The task is handed references, never a payload.** A document id and an organization id, as
strings, for the reason `analyze_ticket` gives: a snapshot copied into the broker is stale by the
time it runs, the broker serializes to JSON so a UUID would arrive as a string regardless, and
converting at this boundary keeps the signature honest about what comes over the wire. The
document's text, its source URL, and its bytes are all read from the database and storage at the
moment of ingestion rather than carried in the message — which is also why deleting a document
between the queue and the run is a clean no-op rather than an error.

**`worker_context` and not a fabricated `TenantContext`.** This process has no authenticated user,
and `WorkerContext` is the type that says so. `knowledge_service.run_ingestion` needs it for two
things and no more: which tenant's row to fill, and whose ledger row the embedding spend lands on.
Inventing a user id and a role here would be inventing an identity — the class of thing ADR-009
and §4 exist against.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import event_loop
from app.core.config import get_settings
from app.core.tenancy import WorkerContext
from app.services import knowledge_service
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

_settings = get_settings()

# No pool, for the reason `ai_tasks` gives: `event_loop.run` builds a new loop per task, and a
# pooled psycopg connection belongs to the loop that opened it — the second task to run would be
# handed a connection from a loop that has closed. `echo` stays off, because a statement log
# carries query parameters and the parameters here are document text.
_engine = create_async_engine(str(_settings.DATABASE_URL), poolclass=NullPool)

_SessionFactory = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    # Readable after commit: `run_ingestion` commits several times — the claim, the chunks, the
    # failure — and it reads the document back between them. A lazy refresh here would raise in
    # async code.
    expire_on_commit=False,
)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.workers.knowledge_tasks.ingest_document",
)
def ingest_document(document_id: str, organization_id: str) -> dict[str, str | int]:
    """Ingest one document. Returns a status and a chunk count, never the document's text.

    The result backend is a debugging aid — the durable record is the `knowledge_documents` row
    and its chunks. A task that returned the extracted text would invite a caller to read a
    document's contents out of Redis instead of out of the table whose access is tenant-scoped,
    and the two returned values answer the only question the backend is good for: did anything
    happen, and how much of it.

    **No retry, and none is wanted.** `ai_service._run` is where §17's retry policy lives and it
    has already applied it to every embedding call this task makes; a second loop at this level
    would multiply the two. A redelivery — which `task_acks_late` makes possible — is handled
    inside `run_ingestion` by the row's status: only a `pending` document is claimed, so a task
    that died after committing its claim finds nothing to re-claim and does not pay to embed the
    document twice.
    """
    return event_loop.run(
        _ingest(
            document_id=uuid.UUID(document_id),
            organization_id=uuid.UUID(organization_id),
        )
    )


async def _ingest(*, document_id: uuid.UUID, organization_id: uuid.UUID) -> dict[str, str | int]:
    """One session for the whole ingestion, opening and closing inside this task's loop.

    `async with` around the session rather than a bare construction, for `ai_tasks._analyze`'s
    reason: `run_ingestion` commits several times, and a session left open would hold a
    connection until the object was collected.
    """
    async with _SessionFactory() as session:
        return await knowledge_service.run_ingestion(
            session,
            WorkerContext(organization_id=organization_id),
            document_id=document_id,
        )
