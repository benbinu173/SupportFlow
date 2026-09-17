"""The process's Redis client.

One client, one lifecycle. A `redis.asyncio` client owns a connection pool, so building
one per call would open and discard a connection each time, and building one per *module*
gives each consumer its own pool, its own reconnect behaviour, and its own answer to "is
Redis up?" — three clients sharing one server and no shared view of it.

Built lazily rather than at import, so importing this module never requires Redis to be
running. `Redis.from_url` parses a URL and constructs a pool object; it does not connect
until the first command is issued.

**This module holds no logger, deliberately.** There is no failure to report here — the
only thing that can go wrong is a connection error, and that surfaces at the call site,
which is the place that knows what to do about it: `app/core/rate_limit.py` fails open
and logs a warning (ADR-014), and `app/api/health.py` reports the dependency as down.
Logging it here as well would report the same event twice, once by something that cannot
act on it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

from app.core.config import get_settings

_shared: Redis | None = None


def get_client() -> Redis:
    """The process-wide client, built on first use.

    **Correct only in a process that owns one long-lived event loop** — the API, whose loop
    lives as long as the process. A Celery task does not: every task reaches async code
    through `app/core/event_loop.run`, which builds *and closes* a loop per invocation, so a
    client cached here during one task would be bound to a loop that no longer exists by the
    next. That task wants `scoped_client` below.

    Not thread-safe in the sense of guaranteeing a single construction under a race —
    the only cost of two callers racing is one discarded pool, and the alternative is a
    lock around every use of a client that is already safe to share.
    """
    global _shared
    if _shared is None:
        _shared = Redis.from_url(str(get_settings().REDIS_URL))
    return _shared


@asynccontextmanager
async def scoped_client() -> AsyncIterator[Redis]:
    """A client for a process that does not own one long-lived loop. Closes when done.

    Phase R is the first consumer. The SLA sweep publishes real-time events from a worker, and
    `event_loop.run` builds a fresh loop for every task invocation — so the task cannot reach
    for the shared client, and it must not leave one behind either. A `redis.asyncio`
    connection captures the loop that opened it, and a client whose loop has closed fails on
    its next command with "attached to a different loop" or "Event loop is closed", neither of
    which names the task that caused it. Worse, the pool's sockets are not closed when the
    loop is — they linger as file descriptors until the objects are collected, which in a
    worker sweeping every five minutes is a slow leak rather than an error anyone would see.

    So the lifetime is explicit and bounded by the call: build, use, close. The cost is one
    connection per task invocation, which for a sweep on a five-minute schedule is nothing
    next to the queries it runs.
    """
    client = Redis.from_url(str(get_settings().REDIS_URL))
    try:
        yield client
    finally:
        await client.aclose()


async def close_client() -> None:
    """Release the pool and forget the client. Called from the app's shutdown hook.

    Clearing `_shared` as well as closing it is what makes the function honest: a
    caller that reaches `get_client()` afterwards gets a working client rather than a
    closed one. That matters in tests, where the process outlives a single app.
    """
    global _shared
    if _shared is not None:
        await _shared.aclose()
        _shared = None
