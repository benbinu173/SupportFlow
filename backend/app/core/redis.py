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

from redis.asyncio import Redis

from app.core.config import get_settings

_shared: Redis | None = None


def get_client() -> Redis:
    """The process-wide client, built on first use.

    Not thread-safe in the sense of guaranteeing a single construction under a race —
    the only cost of two callers racing is one discarded pool, and the alternative is a
    lock around every use of a client that is already safe to share.
    """
    global _shared
    if _shared is None:
        _shared = Redis.from_url(str(get_settings().REDIS_URL))
    return _shared


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
