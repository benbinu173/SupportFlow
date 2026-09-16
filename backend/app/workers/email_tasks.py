"""The email delivery task — the only thing the worker does today.

One task, and it is deliberately the smallest useful one: load a notification by id,
send it, record that it was sent. Everything about *which* notifications exist was
decided in the API's transaction by `app/services/notification_service.py`; this module
only carries one out of the process.

**The task takes an id, never the content.** Two reasons, and the second is the one that
shapes the code. First, a message body copied into the broker is a second copy of a fact
already stored, and the two diverge the moment either changes. Second, and more
importantly, the broker is not durable in the way the database is — a Redis restart
without persistence loses queued messages, and a task carrying its own payload would
mean those notifications were never delivered and nothing anywhere recorded the loss.
Now the row is the record, `emailed_at IS NULL` is the query for "not delivered", and a
lost message is a row somebody can find and re-queue (ADR-023).

**One event loop per task, and therefore no connection pool.** `app/core/event_loop.run`
builds a loop psycopg can drive (ADR-011) and a new one for every call. That is fine for
the connection itself and fatal for a *pooled* one: a psycopg connection is bound to the
loop that opened it, so the second task to run would be handed a connection belonging to
a loop that has since closed, and fail with an error naming neither the loop nor the
pool. The worker therefore builds its own engine with `NullPool`, where the connection is
closed when the session ends and nothing outlives the task that used it.

That engine is a second engine in the process, which `app/core/database.py` warns
against — the warning is about multiplying connections, and this does not. The worker
never imports `database.py`, so the API's pooled engine is not merely unused here, it
does not exist: the worker process holds one connection at a time, for one task.

**The API's engine is deliberately not what the tests run the task against.** A task
executed inside a request — which is what `task_always_eager` does — would try to start
an event loop inside the one already serving that request, and `asyncio` refuses. The
test suite therefore replaces the *enqueue* with a recorder rather than running the task
inline; the task body is exercised on its own, from a context that owns its loop.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import joinedload
from sqlalchemy.pool import NullPool

from app.core import event_loop
from app.core.config import get_settings
from app.core.mail import PermanentEmailError, TransientEmailError, send_email
from app.models.notification import Notification
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

_settings = get_settings()

# See the module docstring: no pool, because a pooled connection would belong to the loop
# that opened it and the next task gets a different loop. `echo` is left off and not
# taken from `DEBUG` — a statement log carries query parameters, and the parameters here
# are a recipient's address.
_engine = create_async_engine(str(_settings.DATABASE_URL), poolclass=NullPool)

_SessionFactory = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    # Readable after commit, so the task can log the row it just stamped rather than
    # triggering a lazy refresh — which raises in async code.
    expire_on_commit=False,
)

# Short enough to read in a log line, long enough that a redelivered send is obvious.
# `delivered` covers "sent just now", the other three are the cases worth counting.
_MISSING = "missing"
_ALREADY_SENT = "already-sent"
_NO_RECIPIENT = "no-recipient"
_DELIVERED = "delivered"


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.workers.email_tasks.send_notification_email",
    # Retry only what retrying can fix. A connection refused or a socket timeout is
    # worth another attempt; a refused recipient or a bad credential is not, and five
    # attempts with backoff would be five minutes spent re-sending a message the server
    # has already rejected. `PermanentEmailError` is not in the tuple, so it fails the
    # task on the first attempt and the traceback lands in the worker's log.
    autoretry_for=(TransientEmailError,),
    # Exponential backoff with jitter. The backoff keeps a downed mail server from being
    # hammered; the jitter spreads a batch of notifications that failed together, which
    # would otherwise retry in lockstep and arrive as a spike.
    #
    # `60` and not the `True` this started as, which means 1 second. The reasoning above
    # is about a mail server that is *down*, and a server down for a restart is down for
    # longer than a second — `retry_backoff=True` spaced all six attempts inside about
    # fifty seconds and gave up while the server was still booting, which is the failure
    # this policy exists to survive. A run against a closed port is what showed it: the
    # observed countdowns were 1s, 1s, 3s, 8s, 10s.
    retry_backoff=60,
    retry_jitter=True,
    # ~1 + 2 + 4 + 8 + 10 minutes across the five retries — the last is the 16 the
    # doubling asks for, capped by Celery's `retry_backoff_max` default of ten minutes.
    # Those are the nominal countdowns; the jitter above draws each one uniformly below
    # its nominal, which is why an observed first retry reads "29s" rather than "60s".
    # Past that, a notification is stale enough that its recipient has already seen the
    # in-app badge, and the row is still there for a sweep to pick up.
    max_retries=5,
)
def send_notification_email(notification_id: str) -> str:
    """Deliver one notification's email. Returns what happened, for the result backend.

    The return value is diagnostic only — nothing reads it — which is why it is a short
    string and not a payload. `emailed_at` on the row is the actual record.

    No `bind=True`: nothing here needs the task instance. `autoretry_for` schedules its
    own retries, and an unused `self` is a parameter every reader has to check.
    """
    return event_loop.run(_deliver(uuid.UUID(notification_id)))


async def _deliver(notification_id: uuid.UUID) -> str:
    """Read, send, record. The async half, run on a loop psycopg can use."""
    async with _SessionFactory() as session:
        # An unscoped read, and the only one in the application outside login's
        # cross-tenant lookup. The worker has no request and therefore no
        # `TenantContext` — but it also has no untrusted input: this id was written
        # by `notification_service` inside a tenant-scoped request, committed, and
        # handed over by the queue. There is no id here a client chose, so there is
        # no tenant predicate missing. Every query the *API* makes stays scoped; see
        # `app/repositories/notification_repository.py` for those.
        result = await session.execute(
            select(Notification)
            .where(Notification.id == notification_id)
            # Joined rather than lazy: an async lazy load raises, and this is the
            # one attribute the task needs.
            .options(joinedload(Notification.user))
        )
        notification = result.scalar_one_or_none()

        if notification is None:
            # The notification was deleted between being queued and being delivered —
            # a user removed, and `notifications.user_id` cascades. Not an error:
            # the alert it described belongs to an account that no longer exists.
            logger.info("notification_delivery_skipped", reason=_MISSING)
            return _MISSING

        # The at-least-once guard. `task_acks_late` means a worker killed after
        # sending but before acknowledging is handed this task again, and without
        # this check it would send a second copy of the same email.
        if notification.emailed_at is not None:
            logger.info(
                "notification_delivery_skipped",
                notification_id=str(notification.id),
                reason=_ALREADY_SENT,
            )
            return _ALREADY_SENT

        recipient = notification.user
        if recipient is None or not recipient.is_active:
            # Deactivated after the notification was written. Sending to a disabled
            # account is mailing somebody the organization has cut off.
            logger.info(
                "notification_delivery_skipped",
                notification_id=str(notification.id),
                reason=_NO_RECIPIENT,
            )
            return _NO_RECIPIENT

        # The address, the title, and the ticket reference. The subject is composed
        # here rather than in the service because a subject line and a bracketed
        # prefix are email's concerns, and the notification row is not an email — it
        # is what the in-app bell shows, and it has to read correctly there.
        try:
            send_email(
                to=recipient.email,
                subject=f"[{_settings.PROJECT_NAME}] {notification.title}",
                body=f"{notification.body}\n\nOpen {_settings.PROJECT_NAME} to view it.",
            )
        except PermanentEmailError as exc:
            # Logged, then raised so Celery records the failure. The exception's
            # message is an exception type name and nothing else — a recipient
            # refusal's own message quotes the address, and a recipient's address in
            # a log is personal data (§4).
            logger.error(
                "notification_delivery_failed",
                notification_id=str(notification.id),
                organization_id=str(notification.organization_id),
                error_type=type(exc).__name__,
                retryable=False,
            )
            raise

        # Stamped only after the send succeeded, so a failed attempt leaves the row
        # looking exactly like one that was never queued — which is what it is, and
        # what a later sweep should pick up.
        #
        # The window this leaves is a crash between the send above and the commit
        # below: mail sent, flag unset, and the redelivered task sends it again. It
        # is narrowed rather than closed, and closing it would need the send and the
        # mark in one atomic step — which SMTP is not part of, so the honest
        # description is "at-least-once, with a window one statement wide".
        notification.emailed_at = datetime.now(UTC)
        await session.commit()

        logger.info(
            "notification_delivered",
            notification_id=str(notification.id),
            # Ids only. Not the address, not the subject.
            organization_id=str(notification.organization_id),
            recipient_id=str(recipient.id),
        )
        return _DELIVERED
