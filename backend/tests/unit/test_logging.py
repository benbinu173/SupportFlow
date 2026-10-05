"""Logging configuration tests.

`configure_logging` is a process-global side effect, so every test here restores what it found.
That is not tidiness: the suite runs with `ENVIRONMENT=test`, which selects the console
renderer, and a test that left the JSON renderer installed would make every later test's log
assertions depend on file ordering.
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from app.core.config import Settings
from app.core.logging import configure_logging

BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "S3_ACCESS_KEY": "minioadmin",
    "S3_SECRET_KEY": "minioadmin",
}


def _settings(**overrides: str) -> Settings:
    return Settings(**{**BASE_ENV, **overrides}, _env_file=None)  # type: ignore[arg-type]


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Put structlog and the root logger back exactly as they were.

    `structlog.get_config()` returns the live keyword arguments `configure` was called with, so
    re-applying them is a true restore rather than a guess at the previous defaults.
    """
    structlog_config = structlog.get_config()
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level

    yield

    structlog.configure(**structlog_config)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def _renderer(processors: list[Any]) -> Any:
    """The last processor, which is by construction the renderer."""
    return processors[-1]


@pytest.mark.unit
def test_production_logs_are_json(restore_logging: None) -> None:
    """A deployed container emits parseable lines; an aggregator needs fields, not prose."""
    configure_logging(_settings(ENVIRONMENT="production", JWT_SECRET="Zx9" * 11))

    processors = structlog.get_config()["processors"]
    assert isinstance(_renderer(processors), structlog.processors.JSONRenderer)


@pytest.mark.unit
def test_development_and_test_logs_stay_readable(restore_logging: None) -> None:
    """The console renderer everywhere else, on `is_production` and not on `DEBUG`.

    A developer reading a terminal wants the coloured one whether or not debug logging is on,
    and the production branch is the exception rather than the default.
    """
    for environment in ("development", "test"):
        configure_logging(_settings(ENVIRONMENT=environment))

        processors = structlog.get_config()["processors"]
        assert isinstance(_renderer(processors), structlog.dev.ConsoleRenderer)


@pytest.mark.unit
def test_the_level_filters_below_it(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`LOG_LEVEL=WARNING` must actually drop an info line, not merely relabel it.

    Asserted through real output rather than by inspecting the wrapper class, because the two
    halves of the setting have to hold together: the line must not be *rendered*, and the call
    must not be *expensive*. A test that reconfigured structlog to capture the event dict would
    pass the first half by accident and lose the second — the capture replaces the filtering
    wrapper along with everything else.
    """
    configure_logging(_settings(LOG_LEVEL="WARNING"))

    logger = structlog.get_logger("test")
    logger.info("this_is_dropped")
    logger.warning("this_is_kept")

    written = capsys.readouterr().out
    assert "this_is_kept" in written
    assert "this_is_dropped" not in written


@pytest.mark.unit
def test_configuring_twice_leaves_one_root_handler(restore_logging: None) -> None:
    """Idempotence, because both entry points in one process is a real path.

    A configure that appended would double every stdlib line — the classic symptom of a
    logging setup that ran once per worker fork.
    """
    configure_logging(_settings())
    first = len(logging.getLogger().handlers)

    configure_logging(_settings())
    second = len(logging.getLogger().handlers)

    assert first == 1
    assert second == 1


@pytest.mark.unit
def test_stdlib_records_are_rendered_by_the_same_chain(restore_logging: None) -> None:
    """A third-party library's line is shaped like this project's.

    SQLAlchemy and boto3 log through the stdlib. Without `ProcessorFormatter` they would emit
    their own format beside our JSON — two shapes in one stream.
    """
    configure_logging(_settings(ENVIRONMENT="production", JWT_SECRET="Zx9" * 11))

    handler = logging.getLogger().handlers[0]
    formatter = handler.formatter

    assert isinstance(formatter, structlog.stdlib.ProcessorFormatter)
    # The renderer the formatter will use, not a second one chosen here — that is the property
    # that keeps the two paths from disagreeing.
    assert isinstance(formatter.processors[-1], structlog.processors.JSONRenderer)
