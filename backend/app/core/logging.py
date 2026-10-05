"""Logging configuration — the one place a renderer and a level are decided.

**This module did not exist until Phase Y, and its absence was a production bug.** Every
module in this codebase has called `structlog.get_logger` since the beginning, but nothing
ever called `structlog.configure`. structlog's *default* configuration is a `ConsoleRenderer`
writing to stdout, which is pleasant in a terminal and wrong in a log aggregator: the lines
arrive as prose with no level a filter can select on, no timestamp in a sortable form, and no
request id to group them by. Nothing bound `contextvars` either, so the `merge_contextvars`
processor sitting in the default chain merged an empty mapping on every call.

**One function, called from both processes.** The API (`app.main.create_app`) and the worker
(`app.workers.celery_app`) are separate processes with separate logger registries, and a worker
that emits console lines into the same aggregator the API fills with JSON is the problem only
half solved. Each calls `configure_logging` in its own process, so there is one answer rather
than two that agree until one is edited.

**`cache_logger_on_first_use=True` is deliberate and has a sharp edge.** It is what makes
`get_logger` calls at module import time cheap, and it means the configuration must be in place
*before* a module-level logger is first used — which is why `create_app` configures at the top
rather than in `lifespan`. `tests/unit/test_logging.py` pins that ordering.

**uvicorn's own loggers are not rewritten here.** uvicorn configures `uvicorn.error` and
`uvicorn.access` with `propagate=False` in its own logging config, so a root handler cannot
reach them; corralling them would mean handing uvicorn a `--log-config` file, which is a
deployment concern rather than an application one, and is noted in the README's deployment
section instead. What this module does own is every `structlog` logger in `app/` — which is all
of this project's logging — and any third-party library that logs through the stdlib.
"""

import logging
import sys
from typing import Any

import structlog

from app.core.config import Settings

# The processors every environment shares, in the order they run.
#
# `merge_contextvars` is first because it is the one that has to happen before the level and
# the timestamp are attached: it pulls in whatever the request-id middleware bound, and a
# processor that ran after the renderer would contribute nothing. `add_log_level` and
# `TimeStamper` are added explicitly rather than left to the renderer, because the JSON path
# and the console path would otherwise disagree about where the level and the time live — and
# that disagreement is exactly what makes a log query written against one environment return
# nothing in the other.
_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    # Exceptions become a string field on the event. Without this, `logger.exception` in
    # production would carry a traceback object the JSON renderer cannot serialise and would
    # drop, which is the one log line a person most needs.
    structlog.processors.format_exc_info,
]


def configure_logging(settings: Settings) -> None:
    """Configure structlog for this process. Idempotent, so both entry points may call it.

    Idempotent matters because it *is* called twice in one process in some paths — the Celery
    worker imports `app.main` transitively through the task modules, and each entry point
    configures — and a configure that appended a handler per call would duplicate every line.
    `structlog.configure` replaces rather than appends, which makes this naturally safe; the
    `disable_existing_loggers=False` below is the stdlib equivalent.

    **JSON in production, console everywhere else.** Not a `DEBUG` flag: a developer reading a
    terminal wants the coloured one whether or not debug logging is on, and a deployed container
    wants the parseable one whether or not it is.
    """
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.is_production
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*_SHARED_PROCESSORS, renderer],
        # A filtering bound logger rather than a plain one: the level check happens once, at
        # the call, so a `logger.debug` under a WARNING level costs a comparison rather than a
        # dict construction the renderer would throw away.
        wrapper_class=structlog.make_filtering_bound_logger(_level_number(settings.LOG_LEVEL)),
        # stdout rather than stderr: this is a container, and the platform reads one stream.
        # `PrintLoggerFactory` is structlog's own, so the four-line `WriteLoggerFactory` dance
        # that only exists to add a timestamp the processors already add is not needed.
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        # See the module docstring — this is why configuration happens before first use.
        cache_logger_on_first_use=True,
    )

    _configure_stdlib(settings, renderer)


def _configure_stdlib(settings: Settings, renderer: Any) -> None:
    """Route third-party stdlib logging through the same renderer.

    SQLAlchemy, botocore, and Celery all log through the stdlib, and left alone they would
    emit their own format beside our JSON — two shapes in one stream, which is the thing this
    module exists to stop. `ProcessorFormatter` is structlog's adapter for exactly this: it
    takes a stdlib record and runs it through the same processor chain.

    **`foreign_pre_chain` re-applies the shared processors, and that is not a copy-paste.** A
    record that arrived through the stdlib never passed through `structlog.configure`'s chain,
    so it has no level field and no ISO timestamp until this chain gives it one. The renderer
    is passed in rather than chosen again so the two paths cannot disagree.

    The root handler is replaced rather than added to, so repeated calls — Celery under a
    reloader, or a test that configures twice — leave one handler and not a growing pile.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED_PROCESSORS,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(_level_number(settings.LOG_LEVEL))

    # uvicorn installs its own handlers and sets `propagate=False`, so its lines reach its own
    # stderr writes and not this handler. Saying so here saves the next reader from concluding
    # the formatter is broken when uvicorn's startup banner looks different from everything else
    # in the stream. Unifying it means handing uvicorn a `--log-config`, which the README's
    # deployment section covers.


def _level_number(level: str) -> int:
    """`"INFO"` → `20`. The stdlib's own table is the single source of truth for the mapping.

    `getLevelNamesMapping` rather than `getLevelName`, whose return type is `int | str` — it
    answers with the *name* for a number it does not recognise, so a typo would sail through
    mypy's `int` and become a string handed to `setLevel`. The mapping is `dict[str, int]` and
    has no such branch. A missing key raises `KeyError`, which is the right failure: the only
    caller passes a value Pydantic has already constrained to a `Literal`.
    """
    return logging.getLevelNamesMapping()[level.upper()]


__all__ = ["configure_logging"]
