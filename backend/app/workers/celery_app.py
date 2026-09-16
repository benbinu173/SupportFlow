"""The Celery application — the worker's entrypoint, and nothing else.

Phase C added `celery` to `pyproject.toml` and Phase P is where a consumer arrived, the
same way Redis sat unused until Phase O. This module is the app object; the tasks live
in `app.workers.email_tasks` and `app.workers.sla_tasks`, both named in `include` below so
that starting a worker with

    celery -A app.workers.celery_app worker

discovers them. Without the import, Celery starts, reports an empty task registry, and
every queued message fails with `Received unregistered task` — a failure that looks like
a broker problem and is actually a missing import.

The short `-A` form is what the Makefile and compose use, and it is verified rather than
assumed: Celery's app discovery falls back to scanning the module for a `Celery`
instance, and `celery -A app.workers.celery_app report` against this module prints the
settings below. The explicit `app.workers.celery_app:celery_app` would work too and is
not needed; what would not work is a shorter path, since the attribute is found *in* the
named module rather than by importing a parent package.

**Why the app and the tasks are separate modules.** `app/main.py` and the API import
services that queue tasks (`notification_service.enqueue_delivery`), and those only ever
*publish* to a broker. Keeping the app in one small module means the import graph that
reaches the API stays small, and the module that configures the worker is not the module
that defines the work.

**`include` is a list rather than imports, so the API process does not build a worker.**
A task module reaches the database through `app/core/event_loop.run` and creates an engine
at import time; importing one from the app object would do that inside every API process
too. Celery imports them in the *worker*, which is the only process that should have them.

Every setting below is a decision with a reason, because Celery's defaults are tuned for
a different kind of deployment — a large, trusted, tasks-internal one. This is an
internet-facing multi-tenant API whose worker sends email outbound, and the defaults are
wrong for several of those in ways that matter (§4, §54).
"""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

# Two queues now, each named for its purpose. §51's Phase P asks for "task routing", and
# the route table's entries are what makes each destination explicit instead of relying on
# the default queue's name — and the line that changes when the next producer arrives. The
# queues that will join them are `ai` (Phases T-W), `reports` (S), and `knowledge` (X) —
# none of which is declared here, because declaring a queue nothing publishes to is a
# worker process waiting for work that does not exist.
NOTIFICATIONS_QUEUE = "notifications"

# Phase Q. Separate from `notifications` because the two have nothing in common: one holds
# a worker slot for up to the SMTP timeout and talks to a mail server, the other finishes
# in seconds and talks only to PostgreSQL. On one queue, a mail server outage delays every
# SLA alert and a slow sweep delays every email — two unrelated failure modes made each
# other's problem.
SLA_QUEUE = "sla"

celery_app = Celery(
    "supportflow",
    broker=str(settings.CELERY_BROKER_URL),
    backend=str(settings.CELERY_RESULT_BACKEND),
    # Both task modules, for the reason the docstring gives: a module that is not imported
    # is not in the registry, and every message for it fails as `Received unregistered
    # task`, which reads like a broker fault.
    include=["app.workers.email_tasks", "app.workers.sla_tasks"],
)

celery_app.conf.update(
    # --- Serialization ------------------------------------------------------
    # JSON only, and `accept_content` pins the same. The Celery default is JSON, but the
    # default is not the point — pickle is one configuration typo away and is remote code
    # execution by design: a worker unpickling a message from the broker runs whatever
    # the payload says. §4 does not allow that, so the allowlist makes it impossible
    # rather than discouraged. It also matters because the broker here is Redis, a
    # service anything on the network can sometimes reach.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # UTC everywhere, matching the database's `timestamptz` columns. Celery's default is
    # the worker's local time, which makes two workers in two timezones disagree about
    # when a task was scheduled.
    timezone="UTC",
    enable_utc=True,
    # --- Delivery guarantees ------------------------------------------------
    # Acknowledge *after* the task finishes, not when it is received. This is what stops
    # a worker killed mid-send from silently losing the email: with the default
    # (`acks_late=False`) the message is gone the moment it is handed over, and a crash
    # during the SMTP conversation loses it forever with nothing anywhere to say so.
    #
    # The price is at-least-once delivery: a worker that dies after sending but before
    # acknowledging is handed the same task again. `notifications.emailed_at` narrows
    # that from "any redelivery duplicates mail" to "a crash between the send and the
    # mark does" (ADR-023).
    task_acks_late=True,
    # Pairs with the above. Without it, a task lost to a killed worker has already been
    # acknowledged and is simply dropped.
    task_reject_on_worker_lost=True,
    # One task in flight per worker process. The default of 4 hands a worker four
    # messages at once, and since a task here can block for the full SMTP timeout, that
    # is four notifications held hostage by one unreachable mail server. One at a time
    # also spreads a burst across the pool instead of piling it on one process.
    worker_prefetch_multiplier=1,
    # Tell Redis not to lose queued work when it is restarted without persistence
    # configured. `visibility_timeout` is Redis's re-delivery window for a message that
    # was never acknowledged — at the default of one hour, an `acks_late` task that
    # outlives it is redelivered while still running. The task time limits below cap the
    # task well inside this, so the window cannot be reached.
    broker_transport_options={"visibility_timeout": 3600},
    # --- Time limits --------------------------------------------------------
    # A hung SMTP conversation must not pin a worker slot forever. Both come from
    # settings so a deployment can tune them without editing code. The soft limit raises
    # inside the task and is catchable; the hard limit kills the process, which is what a
    # socket stuck in a read actually needs.
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT_SECONDS,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT_SECONDS,
    # --- Routing ------------------------------------------------------------
    task_default_queue=NOTIFICATIONS_QUEUE,
    task_routes={
        "app.workers.email_tasks.*": {"queue": NOTIFICATIONS_QUEUE},
        "app.workers.sla_tasks.*": {"queue": SLA_QUEUE},
    },
    # --- Schedule -----------------------------------------------------------
    # Beat's whole configuration, and the first entry this project has ever had: Phase P
    # built the worker but nothing needed a clock, which is why the wiring test asserted
    # this was empty until Phase Q filled it.
    #
    # The interval is a setting because it is the knob that decides how late an alert can
    # be — see `app/core/config.py`. The task name is spelled out rather than referenced,
    # matching the route table above: both are strings the worker and beat must agree on
    # with the decorator, and `tests/integration/test_celery_wiring.py` checks that they do.
    #
    # **Exactly one beat process.** Beat is a singleton by construction — two of them each
    # fire every entry on their own schedule, so the sweep runs twice per interval and
    # every alert is staged twice. Nothing in this file can prevent that; compose runs one
    # `beat` service and the README says not to scale it.
    beat_schedule={
        "sla-deadline-sweep": {
            "task": "app.workers.sla_tasks.check_sla_deadlines",
            "schedule": float(settings.SLA_SWEEP_INTERVAL_SECONDS),
        }
    },
    # --- Worker lifecycle ---------------------------------------------------
    # Do not let Celery replace the root logger's handlers. The application logs through
    # structlog to stdout, and a worker that reconfigures the root logger logs in a
    # different shape from the API — which means one query cannot see both halves of a
    # request's story, and the worker's lines stop matching whatever collector is reading
    # the API's.
    worker_hijack_root_logger=False,
    # Retry the broker connection at startup rather than exiting. In compose the worker
    # and Redis start together, and a worker that gives up because Redis was a second
    # behind is a container that has to be restarted by hand. Celery 6 makes this the
    # default; setting it explicitly keeps the behaviour off the version number.
    broker_connection_retry_on_startup=True,
    # Results are stored so a task's outcome is inspectable while debugging, but nothing
    # reads them — the notification row is the record of what happened, not the result
    # backend. Expiring them keeps Redis from accumulating a result per email forever.
    result_expires=3600,
    # `task_always_eager` is deliberately absent, here and in the test suite. Eager mode
    # makes `.delay()` run the task inline, in the calling frame — and the caller is a
    # request served by the API, so the task body would try to start a second event loop
    # (it reaches the database through `app/core/event_loop.run`) inside the one already
    # serving that request. asyncio refuses, and every request that produced a notification
    # would fail. The suite records the enqueue instead and exercises the task body on its
    # own, from a context that owns its loop — see `tests/conftest.py`'s `queued_emails`.
)
