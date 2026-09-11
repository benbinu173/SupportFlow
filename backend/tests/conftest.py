"""Shared pytest fixtures.

Environment variables are set before importing application code so that
`Settings` validation succeeds without a real .env file present.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault(
    "DATABASE_URL",
    # Matches the credentials in docker/postgres/init: the dev and test databases
    # share a role, and only the database name differs.
    "postgresql+psycopg://supportflow:supportflow@localhost:5432/supportflow_test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("JWT_SECRET", "test-secret-value-that-is-long-enough-32")

# Imported after the environment is populated, which is why this block sits below
# the statements above. Ruff's E402 allows `os.environ` setup before imports
# precisely because this pattern is unavoidable for test configuration.
from app.core.config import get_settings
from app.core.event_loop import loop_factory
from app.main import create_app
from app.models import Base
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Synchronous test client against the real ASGI app.

    `backend_options` carries the same loop factory the app and the async tests
    use. TestClient runs the app on its own loop in a worker thread, which would
    otherwise be a ProactorEventLoop on Windows — enough for routing tests to pass
    while every endpoint touching Postgres silently reported itself unavailable.
    """
    with TestClient(
        create_app(),
        backend_options={"loop_factory": loop_factory},
    ) as test_client:
        yield test_client


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Give every async test a loop the database driver can use.

    psycopg cannot drive Windows' default ProactorEventLoop, so without this every
    database test fails at connect with an InterfaceError — see
    app/core/event_loop.py for the full reasoning.

    This hook rather than the `event_loop_policy` fixture: policies are deprecated
    in Python 3.14 and pytest-asyncio is removing the fixture that wraps them. The
    factory reused here is the same one uvicorn is given, so tests and the running
    app share a loop implementation instead of drifting apart.
    """
    return {"asyncio": loop_factory}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# These fixtures need a running Postgres with the vector and pg_trgm extensions.
# `make up` provides it; tests using them are marked `integration`.


@pytest.fixture(scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Session-scoped engine with the schema created once and dropped at the end.

    Built from metadata rather than by running migrations: this asserts the models
    are self-consistent, independently of whether a migration has been written yet.
    Phase E adds a separate check that migrations produce the same schema.
    """
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    async with engine.begin() as conn:
        # Leftovers from an interrupted run would otherwise fail create_all.
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose writes are rolled back when the test ends.

    The session joins an outer transaction that is never committed, so tests share
    one schema without leaking rows into each other. `session.commit()` inside a
    test commits to a savepoint only, which keeps commit-dependent behaviour
    testable while staying isolated.
    """
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        factory = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as session:
            yield session
        await transaction.rollback()
