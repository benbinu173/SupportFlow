"""The Celery app is configured the way the documentation says it is.

Celery's settings are a loose, string-keyed namespace: `celery_app.conf.update(**kwargs)`
accepts any key at all and does not complain about a misspelling, a wrong type, or a name
that stopped existing two major versions ago. `task_acks_late=True` and
`task_acks_late="True"` and `acks_late=True` are three different outcomes, and only one of
them is the setting that was meant. Nothing in the application imports these keys, so
nothing in the application can catch a typo.

That is what this file is for. Every setting asserted here is one the code explains at
length and a reader would reasonably assume is in force — most of them for security or
durability reasons rather than taste. An assertion against the loaded configuration object
is the only thing that turns "the comment says JSON only" into a fact.

**It contacts no service.** The app object is built at import time from settings, and
every claim below is about that object. The broker is named here, not dialled — which is
why the marker says `integration` (the subject is the broker's configuration) while the
suite's `integration` tests that need a live Postgres are in other files.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.workers.celery_app import NOTIFICATIONS_QUEUE, SLA_QUEUE, celery_app

pytestmark = pytest.mark.integration

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# The two URLs
# ---------------------------------------------------------------------------


def test_the_broker_and_the_result_backend_are_the_configured_ones() -> None:
    """The URLs in the app object come from settings, with nothing overriding them in code.

    Worth pinning because these two are the only settings the app takes as constructor
    arguments rather than through `conf.update`, so a stale literal here would not be
    visible anywhere else.
    """
    settings = get_settings()

    assert str(celery_app.conf.broker_url) == str(settings.CELERY_BROKER_URL)
    assert str(celery_app.conf.result_backend) == str(settings.CELERY_RESULT_BACKEND)


def test_the_defaults_separate_the_cache_the_tests_and_the_broker() -> None:
    """ADR-022's database split: cache in 0, tests in 1, Celery in 2.

    Read off the **field defaults** rather than the live configuration, because the live
    one deliberately does not show it: the suite points `REDIS_URL` and `CELERY_BROKER_URL`
    at the same throwaway database, on the grounds that a test run has no reason to keep
    them apart and one database to flush is simpler than three. What the split is
    documented as is what a deployment starts from, and that is the defaults.

    `REDIS_URL` is the one of the three with no default — it is a required setting, so the
    cache's database number lives only in the environment and in `.env.example`, which the
    next test covers.

    The separation is the load-bearing part — it is what stops a `FLUSHDB` from a test run
    clearing a live worker's queue — and it is invisible in code, because the only thing
    that expresses it is the number at the end of a URL.
    """
    fields = type(get_settings()).model_fields

    broker = make_url(str(fields["CELERY_BROKER_URL"].default))
    backend = make_url(str(fields["CELERY_RESULT_BACKEND"].default))

    assert (broker.database, backend.database) == ("2", "2")


def test_the_example_environment_keeps_the_three_databases_apart() -> None:
    """`.env.example` is the file a new deployment copies, so it is where the split is real.

    Parsed rather than grepped, so a commented-out line or a reordered file cannot satisfy
    it by accident.

    Both properties are asserted, because they answer different questions. The literals pin
    ADR-022's convention, so a phase that quietly moved Celery to db 3 fails here rather
    than in a runbook. The distinctness is the property a future change might break while
    keeping plausible-looking numbers — and the one that matters, since a cache and a queue
    sharing a database means a `FLUSHDB` in one deletes the other's contents.

    Nothing reads all three URLs at once, so nothing in the application could notice them
    converging; this file is the only place that can.
    """
    lines = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    example = {
        key.strip(): value.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
        for key, _, value in [line.partition("=")]
    }

    databases = {
        name: make_url(example[name]).database
        for name in ("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND")
    }

    assert databases == {
        "REDIS_URL": "0",
        "CELERY_BROKER_URL": "2",
        "CELERY_RESULT_BACKEND": "2",
    }
    assert len(set(databases.values())) == 2, "the cache must not share a database with Celery"


# ---------------------------------------------------------------------------
# Serialization — the RCE one
# ---------------------------------------------------------------------------


def test_only_json_can_be_serialized_or_accepted() -> None:
    """Pickle is remote code execution by design, and this is the setting that forbids it.

    All three keys matter and they are not redundant. `task_serializer` and
    `result_serializer` decide what this application *produces*; `accept_content` decides
    what it will *unpickle*, and that is the direction an attacker controls — the worker
    reads from a broker that anything on the network can sometimes write to. A worker
    accepting pickle runs whatever the payload's `__reduce__` says.

    Asserted as equality rather than as "pickle is absent", so a future addition of some
    third format has to be a deliberate edit here as well as there.
    """
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


def test_a_task_is_acknowledged_only_after_it_finishes() -> None:
    """The pair that stops a killed worker from silently losing an email.

    `acks_late` alone is not enough: without `task_reject_on_worker_lost`, a message whose
    worker was killed has already been acknowledged and is dropped rather than requeued.
    The two are asserted together because enabling one of them is the shape of the bug —
    the configuration reads as if it is safe and is only half of it.
    """
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_the_visibility_timeout_outlasts_the_task_time_limit() -> None:
    """A redelivery window shorter than a task leaves two workers sending the same email.

    Redis redelivers an unacknowledged message once `visibility_timeout` passes, and with
    `acks_late` a long-running task is unacknowledged for its whole duration. If the
    hard time limit were ever raised past the timeout, every slow send would be picked up
    by a second worker — and `emailed_at` narrows that but does not close it.
    """
    assert celery_app.conf.broker_transport_options == {"visibility_timeout": 3600}
    assert celery_app.conf.task_time_limit < 3600


def test_the_time_limits_come_from_settings_and_the_hard_one_is_higher() -> None:
    """A hung SMTP conversation must not pin a worker slot forever.

    The soft limit raises inside the task and is catchable; the hard limit kills the
    process. A hard limit below the soft one would make the soft limit unreachable, which
    is the kind of ordering mistake a comment cannot prevent.
    """
    settings = get_settings()

    assert celery_app.conf.task_soft_time_limit == settings.CELERY_TASK_SOFT_TIME_LIMIT_SECONDS
    assert celery_app.conf.task_time_limit == settings.CELERY_TASK_TIME_LIMIT_SECONDS
    assert celery_app.conf.task_soft_time_limit < celery_app.conf.task_time_limit


def test_one_worker_process_takes_one_task_at_a_time() -> None:
    """The default of 4 hands a worker four messages at once.

    Since a task here can block for the whole SMTP timeout, four in flight means four
    notifications held hostage by one unreachable mail server.
    """
    assert celery_app.conf.worker_prefetch_multiplier == 1


# ---------------------------------------------------------------------------
# Routing, and the queues that are deliberately not here
# ---------------------------------------------------------------------------


def test_every_task_route_names_a_queue_that_exists() -> None:
    """§51's Phase P asks for task routing, and Phase Q adds the second queue.

    Two queues, each named for its purpose, and the route table's entries are what make each
    destination explicit instead of relying on the default queue's name. The queues that
    will join them are `ai` (Phases T-W), `reports` (S), and `knowledge` (X) — none of which
    is declared here, because declaring a queue nothing publishes to is a worker process
    waiting for work that does not exist.

    Asserted as an exact table rather than as "the expected entries are present", so a route
    added without a queue to serve it — or a queue added without a route publishing to it —
    fails here. The second is the more expensive mistake: an empty queue looks identical to
    a busy one from the outside.
    """
    assert celery_app.conf.task_default_queue == NOTIFICATIONS_QUEUE

    routes = celery_app.conf.task_routes
    assert routes == {
        "app.workers.email_tasks.*": {"queue": NOTIFICATIONS_QUEUE},
        "app.workers.sla_tasks.*": {"queue": SLA_QUEUE},
    }
    assert {route["queue"] for route in routes.values()} == {NOTIFICATIONS_QUEUE, SLA_QUEUE}


def test_the_worker_command_names_every_routed_queue() -> None:
    """The two-queue trap, closed by reading the two files that start a worker.

    A Celery worker consumes the queues it is told to and nothing else. With one queue that
    is invisible — the default is the only queue that exists, so a missing `-Q` still
    works. With two, a worker started without `-Q sla` **consumes nothing at all** while
    looking perfectly healthy: it connects, reports ready, and leaves every SLA task sitting
    in Redis forever. There is no error, no metric, and no log line, because from Celery's
    point of view nothing is wrong.

    That is a deployment failure this suite cannot reach — it starts no worker — so it is
    checked where it is decided instead. Both files are parsed rather than grepped for a
    literal string: the claim is "the command names every queue in `task_routes`", and a
    future phase adding a third queue has to be answered in both places. Comments are
    skipped, so this is the command and not a sentence about it.
    """
    routed = {route["queue"] for route in celery_app.conf.task_routes.values()}

    compose = _declared_queues(REPOSITORY_ROOT / "docker-compose.yml")
    makefile = _declared_queues(REPOSITORY_ROOT / "Makefile")

    assert routed == compose, "docker-compose.yml's worker does not consume every queue"
    assert routed == makefile, "the Makefile's worker does not consume every queue"


def _declared_queues(path: Path) -> set[str]:
    """Every queue name given to a `-Q` flag in a file, ignoring commented-out lines.

    A set rather than a list, because the same command appears in more than one place — the
    Makefile's `worker` and `beat`-adjacent targets, compose's `worker` service — and what
    matters is that each of them names all of them. Duplicates are the expected case.
    """
    declared: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        for value in re.findall(r"-Q\s+([\w,]+)", line):
            declared.update(value.split(","))
    return declared


def test_the_email_task_is_in_the_registry_under_the_routed_name() -> None:
    """The route is a prefix match, so the name registered in the broker has to match it.

    A task left with Celery's generated name would still be routed here by accident —
    the generated name happens to start with the module path — and the point of an
    explicit `name=` is that it is routed by intent. This is where a rename is caught: if
    the registered name stops matching the route pattern, the task is silently sent to the
    default queue instead, and the worker listening on `notifications` never sees it.

    Identity is checked with `==` and not `is`, and that is not a style choice: the module
    attribute is a `celery.local.PromiseProxy`, which resolves to the real task lazily, so
    the object in the registry is genuinely a different object from the one the module
    exports. `is` would fail here with both sides printing the same repr.
    """
    from app.workers.email_tasks import send_notification_email

    registered = celery_app.tasks[send_notification_email.name]
    assert registered == send_notification_email
    assert registered.name == "app.workers.email_tasks.send_notification_email"

    matched = [
        pattern
        for pattern in celery_app.conf.task_routes
        if send_notification_email.name.startswith(pattern.rstrip("*"))
    ]
    assert matched == ["app.workers.email_tasks.*"]


def test_the_task_module_is_imported_so_the_worker_can_find_it() -> None:
    """Without this, the worker starts, reports an empty registry, and fails every message.

    `Received unregistered task of type 'app.workers.email_tasks.send_notification_email'`
    looks like a broker problem and is a missing import — the app object never imported the
    module that defines the task, so Celery's autodiscovery had nothing to find.
    """
    from app.workers.email_tasks import send_notification_email

    assert "app.workers.email_tasks" in celery_app.conf.include
    assert send_notification_email.name in celery_app.tasks


# ---------------------------------------------------------------------------
# Deliberate absences
# ---------------------------------------------------------------------------


def test_the_beat_schedule_is_exactly_the_sla_sweep() -> None:
    """§48's "celery beat where needed", and Phase Q is where it is needed.

    Phase P had no schedule and this file asserted the emptiness; the comment on the old
    test said the first thing needing a clock was SLA monitoring and that the beat service
    would arrive with it. Both halves of that are now true, so the assertion flips from "no
    schedule" to "this schedule and no other" — an exact dict rather than a membership
    check, because an entry added without a beat container to run it is a feature that
    silently never fires.

    The task name is a string in the schedule and a string in the decorator, and nothing in
    the application relates the two: Celery resolves the name at fire time and fails on a
    typo by logging a `NotRegistered` error into beat's output, where nobody is looking. The
    lookup below closes that.

    **The import is what a worker's `include` does**, and it is here rather than at module
    scope for the reason this whole file gives — importing a task module builds an engine,
    and only the test that needs the registry should do it. `include` itself is asserted by
    the test further down; this one is about the *name* in the schedule matching a task the
    module actually registered.
    """
    from app.workers import sla_tasks  # noqa: F401  (registers the tasks, as `include` does)

    schedule = celery_app.conf.beat_schedule

    assert list(schedule) == ["sla-deadline-sweep"]
    entry = schedule["sla-deadline-sweep"]
    assert entry["task"] == "app.workers.sla_tasks.check_sla_deadlines"
    assert entry["task"] in celery_app.tasks, "beat would fire a task nobody registered"


def test_the_sweep_runs_on_the_configured_interval() -> None:
    """The interval comes from settings, because it is the knob that decides how late a
    warning can be.

    At the shortest §27 target — URGENT, 30 minutes, warning at 80% = 24 minutes — a
    5-minute sweep bounds the delay at 5 minutes; at the longest (LOW, 24 hours = 19.2 hours
    to its warning) the same interval is noise. Read through `float()` because Celery
    accepts a number of seconds or a `timedelta`, and a setting changed to the latter would
    otherwise pass by not being compared at all.
    """
    settings = get_settings()

    entry = celery_app.conf.beat_schedule["sla-deadline-sweep"]
    assert float(entry["schedule"]) == float(settings.SLA_SWEEP_INTERVAL_SECONDS)


def test_the_sla_tasks_are_registered_under_their_routed_names() -> None:
    """Two tasks, both matching the route pattern, both in the registry.

    The same argument the email task's registry test makes: a task left with Celery's
    generated name would still be routed here by accident — the generated name starts with
    the module path — so the point of the explicit `name=` is that it is routed by intent.
    A rename that stopped matching the pattern would send the task to the default queue, and
    the worker listening on `sla` would never see it.

    `==` rather than `is`, for the reason the email task's test spells out: the module
    attribute is a `celery.local.PromiseProxy`.
    """
    from app.workers import sla_tasks

    for task, short_name in (
        (sla_tasks.check_sla_deadlines, "check_sla_deadlines"),
        (sla_tasks.check_organization_sla, "check_organization_sla"),
    ):
        registered = celery_app.tasks[task.name]
        assert registered == task
        assert task.name == f"app.workers.sla_tasks.{short_name}"
        assert task.name.startswith("app.workers.sla_tasks."), "the route would not match"


def test_the_sla_task_module_is_imported_so_the_worker_can_find_it() -> None:
    """Without this, every SLA task fails as `Received unregistered task`.

    Which reads like a broker fault and is a missing import: `include` is a list of module
    paths rather than imports, so a module that is not named there is never imported in the
    worker and never registers its tasks.
    """
    assert "app.workers.sla_tasks" in celery_app.conf.include


def test_the_worker_does_not_replace_the_root_logger() -> None:
    """The API logs structlog to stdout, and a worker that reconfigures the root logger does not.

    The consequence is not cosmetic: one query cannot see both halves of a request's story
    if the two processes emit different shapes, and the worker's lines stop matching
    whatever collector is reading the API's.
    """
    assert celery_app.conf.worker_hijack_root_logger is False


def test_the_task_is_not_always_eager() -> None:
    """Eager mode would run `.delay()` inline, inside the request that produced the notification.

    The task body reaches the database through `app/core/event_loop.run`, which starts a
    loop psycopg can drive (ADR-011). Starting a second loop inside the one already serving
    the request raises before a single query is issued, so turning this on would fail every
    request that produced a notification rather than merely testing differently. The suite
    records the enqueue instead; see `tests/conftest.py`.
    """
    assert not celery_app.conf.task_always_eager
