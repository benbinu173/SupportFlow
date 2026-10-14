"""The abandoned-claim sweep — what recovers a row whose worker never came back.

**The problem this exists for, stated once.** `run_analysis` and `run_ingestion` both claim a
row — one conditional `UPDATE` to `processing` — and commit *before* the slow part, which is a
provider call or a page fetch. Committing there is deliberate: it is what makes "running" a
state another process can observe, and what makes a redelivered message a no-op instead of a
second bill. The price is that the claim outlives the process that took it. A worker killed by
the out-of-memory killer, a container stopped mid-task, or the hard time limit firing leaves
the row `processing` with nothing left to finish it — the task was acknowledged rather than
redelivered (`task_acks_on_failure_or_timeout` is left at its default), and nothing else in
this system looks at a `processing` row.

The consequences are not symmetrical and the worse one is the analysis. `request_analysis`
reads `processing` as **in flight**, so the ticket's analysis for that operation can never be
asked for again — the button is refused forever with nothing on any screen to explain it. A
document is quieter and just as final: retrieval requires `status = 'completed'`, no route
re-queues an ingestion, and a `processing` document therefore answers nothing while looking
like it is on its way.

**A sweep rather than a wider `except` clause, and this is the decision worth reading.** Only
one of the four ways a claim gets abandoned — a database error mid-run — is even in principle
catchable by the process holding the claim. The other three are the process ceasing to exist.
A handler for the catchable one would also have to unwind a session that a failed statement may
have poisoned, and SQLAlchemy expires every object in a session on rollback, so the "mark it
failed" write would itself have to avoid touching any ORM instance it had loaded. That is real
complexity bought for one case out of four. One sweep covers all four, and it is the mechanism
the codebase already has for work with no caller — `sla_tasks` is the first, this is the second.

**`failed`, not `pending`.** Putting the row back to `pending` sounds like self-healing and
isn't: nothing re-queues a `pending` row, so it would sit there instead, and a re-enqueue would
be a second scheduled action with a loop to worry about on a row that reliably kills whatever
picks it up. `failed` with a sentence is the truthful record, and it restores the two things
that matter — the operation can be requested again, and the document appears on the admin's
list as something that did not work.

**One organization per sweep is deliberately not the shape here.** `check_sla_deadlines` fans
out per tenant because the work inside one tenant is unbounded (a page of tickets, each with a
policy evaluation and possibly two notifications). This sweep is two `UPDATE`s with an indexed
predicate on `(status, updated_at)`, and the number of rows is however many workers died since
the last pass — normally none. Fanning out would add a task per organization to discover that
every one of them had nothing to do.

**It runs on its own queue** for the reason every other queue here is separate: the profile is
its own. It is database-only and finishes in milliseconds, and the queues it could have joined
carry work that is not — a provider call, an SMTP conversation — so a deployment that wants to
pause analysis spend must not be pausing the thing that un-wedges analysis. The price of a
queue is the classic Celery trap, a worker started without `-Q maintenance` consumes nothing
from it silently, so `tests/integration/test_celery_wiring.py` reads the two files that start a
worker and asserts the flag names every routed queue.
"""

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import event_loop
from app.core.config import get_settings
from app.repositories import ai_repository, knowledge_repository
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

_settings = get_settings()

# No pool, for the reason `sla_tasks` gives: `event_loop.run` builds a new loop per task, and a
# pooled psycopg connection belongs to the loop that opened it. `echo` stays off for the reason
# the rest of the workers give — a statement log carries query parameters, and a parameter here
# is a tenant's row id.
_engine = create_async_engine(str(_settings.DATABASE_URL), poolclass=NullPool)

_SessionFactory = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    # Nothing here is read after the commit, so this is the same default the other task modules
    # set rather than a requirement of this one. It is stated because the alternative is a lazy
    # refresh in async code, which raises rather than loading.
    expire_on_commit=False,
)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.workers.maintenance_tasks.reap_abandoned_claims",
)
def reap_abandoned_claims() -> dict[str, int]:
    """Beat's entry point: fail every claim that outlived the worker which took it.

    Returns counts rather than a message, following `check_sla_deadlines`: the durable record is
    the rows, and the result backend is a debugging aid.

    **No retry, and none is wanted.** Nothing about this task is one-shot — the next beat cycle
    sweeps whatever this one failed to reach, and the rows it is looking for only become
    eligible with age. A retry would also have nothing better to do the second time.
    """
    return event_loop.run(_reap())


async def _reap() -> dict[str, int]:
    """Both tables, one pass, one commit.

    **One commit across the two `UPDATE`s rather than one each.** They are independent, so
    either grouping is correct; together is one round-trip instead of two and it cannot leave
    the log claiming a reap that a later failure rolled back — which is the same reason
    `sla_tasks._sweep` commits a whole organization at once.

    `now` is taken once and the cutoff derived from it, so the two statements judge rows against
    the same instant. Two calls to `datetime.now` a few microseconds apart would be harmless
    here and are the shape that becomes a bug the moment a threshold is tight enough to matter;
    `sla_tasks._sweep` takes its clock at the top for the same reason.

    Nothing is published and nothing is announced. These rows have no requester waiting: the
    user who asked for an analysis is looking at a ticket whose row says `failed`, and the admin
    who uploaded a document is looking at a list that says the same. A socket event about a
    cleanup would be an interruption about a change nobody made.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=_settings.ABANDONED_CLAIM_AFTER_SECONDS)

    async with _SessionFactory() as session:
        analyses = await ai_repository.fail_abandoned_claims(session, claimed_before=cutoff)
        documents = await knowledge_repository.fail_abandoned_documents(
            session, claimed_before=cutoff
        )
        await session.commit()

    for claim in analyses:
        logger.warning(
            "ai_analysis_abandoned",
            analysis_id=str(claim.analysis_id),
            organization_id=str(claim.organization_id),
            ticket_id=str(claim.ticket_id),
            operation=str(claim.operation),
            claimed_before=cutoff.isoformat(),
        )

    for document in documents:
        logger.warning(
            "knowledge_ingestion_abandoned",
            document_id=str(document.document_id),
            organization_id=str(document.organization_id),
            source_type=str(document.source_type),
            claimed_before=cutoff.isoformat(),
        )

    counts = {"analyses": len(analyses), "documents": len(documents)}
    logger.info("abandoned_claims_reaped", **counts)
    return counts
