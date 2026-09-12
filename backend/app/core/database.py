"""Database engine, session factory, and the request-scoped session dependency.

One engine per process, created at import. The engine holds the connection pool, so
constructing more than one would multiply real Postgres connections and quietly
exceed the server's limit under load.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

settings = get_settings()

engine: AsyncEngine = create_async_engine(
    settings.sqlalchemy_dsn,
    # Echo SQL only when explicitly debugging: statement logs contain query
    # parameters, which include customer data.
    echo=settings.DEBUG,
    # Verify a connection before handing it out. Costs a round trip, but without it
    # a connection dropped by a restarted database or an idle-timeout proxy
    # surfaces as a failed request instead of being transparently replaced.
    pool_pre_ping=True,
    # Recycle below the typical 5-minute idle cut-off used by managed Postgres and
    # connection proxies, so the pool never hands out a server-closed socket.
    pool_recycle=280,
    pool_size=10,
    max_overflow=5,
)

SessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    # Attributes stay readable after commit. With the default, every loaded
    # attribute is expired on commit and touching one triggers a lazy refresh —
    # which raises in async code, since the implicit IO cannot be awaited. This is
    # what lets a service commit and then return the ORM object it just wrote.
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession]:
    """Yield a session scoped to one request.

    Commit is the caller's responsibility, not this dependency's: an endpoint that
    raises after a partial write must not have that write committed on its way out.
    Rollback on exception is handled here, since it has to happen regardless of
    which layer raised.
    """
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close every pooled connection. Called from the app's shutdown hook."""
    await engine.dispose()
