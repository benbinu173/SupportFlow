"""The SLA sweep — the first work in this project that runs on a schedule.

**Why beat exists at all.** Everything before Phase P was request-driven: a request
arrives, work happens, a response goes back. Phase P added a worker, but the worker only
consumed tasks somebody else created. An SLA warning is the first thing whose trigger is
*the passage of time* — no request causes it, no user is waiting for it, and the only
thing that can notice it is a clock. That is `celery beat`, and this module is the single
schedule it runs (ADR-024).

**The sweep has no requester, and that shapes every query it makes.** There is no token,
no `TenantContext`, and no authenticated identity. Rather than fabricate one, this module
reads through `app/repositories/sla_repository.py` — module-level functions that take
`organization_id` explicitly, following `audit_service.record` and
`find_users_by_email_across_tenants`. That module's docstring carries the full argument;
the short version is that a fabricated context would carry a user id that is not a user
and a role that decides permissions and describes nobody.

**Two tasks, not one, so one tenant's backlog cannot hold up another's.** `check_sla_deadlines`
finds the active organizations and hands each to `check_organization_sla` as its own task.
The alternative — one task looping every tenant in the fleet — puts every organization
behind the slowest one's batch limit, and reports a failure against no tenant in
particular. This is also the fan-out the report and knowledge phases reuse.

**It runs on its own queue.** `notifications` holds a worker slot for up to the SMTP
timeout; the sweep touches no SMTP and wants to finish in seconds. Sharing one queue means
a mail server outage delays every alert and a slow sweep delays every email. The price of
two queues is the classic Celery trap — a worker started without `-Q sla` consumes nothing
and the tasks pile up invisibly — so `tests/integration/test_celery_wiring.py` reads
`docker-compose.yml` and the `Makefile` and asserts the worker's `-Q` names every routed
queue.

**The idempotency guard is a read, not a second query.** `sla_repository.find_alerts`
fetches the ticket page's SLA timeline entries in one `IN (…)`, and `sla_service.index_alerts`
turns them into "already warned, already breached". Those same rows are what the API
reports as `warned_at` and `breached_at`, so one query serves the guard and the display,
and the two cannot disagree about what has been said. What that buys is the at-most-four
property: two timers, two states, and nothing fires twice.

**No audit row.** §34's list has no SLA-alert action and `AuditAction` has no member for
one. The timeline entry *is* the record, which is where a human looks for it anyway.
`SLA_POLICY_UPDATED`, which §34 does name, is written by the admin's PATCH route.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import event_loop
from app.core.config import get_settings
from app.models.enums import TicketEventType
from app.models.notification import Notification
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.repositories import sla_repository
from app.repositories.organization_repository import OrganizationRepository
from app.services import notification_service, sla_service
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

_settings = get_settings()

# No pool, for the reason `email_tasks` gives: `event_loop.run` builds a new loop per task,
# and a pooled psycopg connection belongs to the loop that opened it, so the second task to
# run would be handed a connection from a loop that has closed. `echo` stays off — a
# statement log carries query parameters, and the parameters here are ticket subjects.
_engine = create_async_engine(str(_settings.DATABASE_URL), poolclass=NullPool)

_SessionFactory = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    # Readable after commit, so a staged notification's id can be handed to the delivery
    # task without a lazy refresh — which raises in async code.
    expire_on_commit=False,
)

# Short strings for the result backend and the log, in the style `email_tasks` uses. There
# is one reason worth naming: a tenant whose SLA is switched off entirely has no active
# policies, and that is a configuration rather than a failure.
_NO_POLICIES = "no-policies"


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.workers.sla_tasks.check_sla_deadlines",
)
def check_sla_deadlines() -> dict[str, int]:
    """Beat's entry point: find every active organization and sweep each one separately.

    Returns counts rather than a message, for the same reason `send_notification_email`
    returns a word: the result backend is a debugging aid, and what actually happened is
    in the timeline, the notification rows, and the log.

    **A broker failure for one tenant does not stop the others**, following
    `enqueue_delivery`: the exception is logged by *type* and without a traceback, because
    a connection error's message embeds the broker URL and in production that URL carries
    a password (§4 — no secrets in logs). The next sweep cycle picks up whatever was
    skipped, since nothing about this task is one-shot.

    The dispatch is deliberately outside the event loop that read the ids: publishing to a
    broker is synchronous, and there is no reason to block a database loop while Redis is
    talked to. `check_organization_sla.delay` is also the seam the tests replace, exactly
    as they replace `send_notification_email.delay`.
    """
    organization_ids = event_loop.run(_active_organization_ids())

    dispatched = 0
    for organization_id in organization_ids:
        try:
            check_organization_sla.delay(str(organization_id))
        except Exception as exc:
            logger.warning(
                "sla_sweep_not_dispatched",
                organization_id=str(organization_id),
                error_type=type(exc).__name__,
            )
            continue
        dispatched += 1

    logger.info(
        "sla_sweep_dispatched",
        organizations=len(organization_ids),
        dispatched=dispatched,
    )
    return {"organizations": len(organization_ids), "dispatched": dispatched}


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.workers.sla_tasks.check_organization_sla",
)
def check_organization_sla(organization_id: str) -> dict[str, int]:
    """Sweep one organization. Takes an id, never a payload.

    The same argument `send_notification_email` makes: the task is handed a reference and
    reads the rest itself. A snapshot of a tenant's tickets copied into the broker would
    be stale by the time it ran, and a Redis restart without persistence would lose it
    while the database still showed nothing had been said.

    Why an id and not a `uuid.UUID`: the broker serializes to JSON, so a uuid would arrive
    as a string regardless. Converting here rather than declaring a `str` the body then
    parses keeps the task signature honest about what comes over the wire.
    """
    return event_loop.run(_sweep(uuid.UUID(organization_id)))


async def _active_organization_ids() -> list[uuid.UUID]:
    """Every organization whose SLA should be swept.

    Active only, which is `OrganizationRepository.list_active_ids` — a suspended tenant's
    tickets are not racing a deadline the tenant is still contractually held to, and
    alerting their staff about one would be noise during whatever caused the suspension.
    """
    async with _SessionFactory() as session:
        return await OrganizationRepository(session).list_active_ids()


async def _sweep(organization_id: uuid.UUID) -> dict[str, int]:
    """One organization's pass: find candidates, ask the clock, stage what is newly due.

    **One commit for the whole organization.** Every timeline entry and every notification
    row for this tenant lands together or not at all, so the sweep is never in a state
    where an alert was recorded on the timeline and nobody was notified — which would be
    permanent, since the guard reads the timeline.

    `enqueue_delivery` runs *after* that commit and outside the session, because the
    delivery task's first act is to read the notification row it was handed the id of.

    `now` is taken once, at the top. A sweep that called `datetime.now` per ticket would
    evaluate a hundred-ticket page against a hundred subtly different instants, and a
    ticket could be judged on track while the one behind it in the same run breached.
    """
    counts = {"tickets": 0, "warnings": 0, "breaches": 0, "notifications": 0, "queued": 0}

    async with _SessionFactory() as session:
        policies = await sla_repository.load_policies(session, organization_id)
        if not policies:
            # Every priority switched off, or a tenant configured outside the application.
            # Not a failure: a clock with no targets has nothing to say.
            logger.info(
                "sla_sweep_skipped",
                organization_id=str(organization_id),
                reason=_NO_POLICIES,
            )
            return counts

        now = datetime.now(UTC)
        notifications: list[Notification] = []

        for priority, policy in policies.items():
            created_before = now - sla_service.earliest_alert_offset(policy)
            candidates = await sla_repository.find_pending(
                session,
                organization_id,
                priority=priority,
                created_before=created_before,
                limit=_settings.SLA_SWEEP_BATCH_SIZE,
            )
            if not candidates:
                continue

            alerts = sla_service.index_alerts(
                await sla_repository.find_alerts(
                    session,
                    organization_id,
                    ticket_ids=[ticket.id for ticket in candidates],
                )
            )

            for ticket in candidates:
                counts["tickets"] += 1
                position = sla_service.resolve_position(ticket, policy, now=now)
                recorded = alerts.get(ticket.id) or sla_service.RecordedAlerts()

                for alert in sla_service.due_alerts(position, recorded):
                    _record(session, organization_id, ticket, alert)
                    notifications.extend(
                        await notification_service.notify_sla_alert(
                            session,
                            organization_id=organization_id,
                            ticket=ticket,
                            alert=alert,
                        )
                    )
                    alert_key = (
                        "warnings"
                        if alert.event_type is TicketEventType.SLA_WARNING
                        else "breaches"
                    )
                    counts[alert_key] += 1
                    logger.info(
                        "sla_alert_recorded",
                        organization_id=str(organization_id),
                        ticket_id=str(ticket.id),
                        event_type=str(alert.event_type),
                        timer=str(alert.timer),
                        due_at=alert.due_at.isoformat(),
                    )

        counts["notifications"] = len(notifications)
        await session.commit()

    # Outside the session: the rows are committed, which is what makes the ids the delivery
    # tasks are handed readable. See `enqueue_delivery`'s docstring for why this order is
    # not negotiable.
    counts["queued"] = notification_service.enqueue_delivery(notifications)

    logger.info("sla_sweep_complete", organization_id=str(organization_id), **counts)
    return counts


def _record(
    session: AsyncSession, organization_id: uuid.UUID, ticket: Ticket, alert: sla_service.SLAAlert
) -> None:
    """Append one SLA alert to the ticket's timeline. **Never commits.**

    Built here rather than through `ticket_service.record_event`, which needs a
    `TenantContext` to name its actor. There is no actor: `actor_user_id` is left `None`,
    which is the state the column's own comment describes — "NULL when the system acted
    rather than a person — SLA breaches and completed AI analyses have no actor".

    `extra_data` carries the deadline the alert fired against, as an ISO 8601 string
    because JSONB is serialized through `json.dumps`, which has no datetime encoder. It is
    **for display only and is never parsed back** — the same rule `TicketEvent.from_value`
    carries. The reason to store it at all: a policy edited next month must not rewrite
    what the timeline says happened, and "the first response was due at 14:32" is a fact
    about a deadline that has since moved.
    """
    event = TicketEvent(
        organization_id=organization_id,
        ticket_id=ticket.id,
        event_type=alert.event_type,
        actor_user_id=None,
        extra_data={"timer": str(alert.timer), "due_at": alert.due_at.isoformat()},
    )
    session.add(event)
