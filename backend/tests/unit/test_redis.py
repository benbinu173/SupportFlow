"""The process's Redis client: built once, released once, rebuildable after.

`app/core/redis.py` exists because three places were creating Redis connections — the
rate limiter behind a private global, and the readiness probe with a fresh
`Redis.from_url` on every poll. This file covers what that consolidation is supposed to
deliver: one object serves every consumer, one shutdown path releases it, and a caller
that arrives afterwards gets a working client rather than a closed one.

No server is needed anywhere here. `Redis.from_url` parses a URL and builds a pool
object; it opens no socket until the first command — which is exactly the property that
lets the module be lazy, and the reason these tests can assert identity rather than
reachability.
"""

from collections.abc import Iterator

import pytest
from redis.asyncio import Redis

from app.core import redis as redis_module
from app.core.rate_limit import RateLimiter
from app.core.redis import close_client, get_client

pytestmark = pytest.mark.unit


@pytest.fixture
def restore_shared() -> Iterator[None]:
    """Put the module's global back as it was, whatever a test did to it.

    Needed because the global genuinely is global — a test that leaves a stub or a
    released client behind would change what every later test in the run sees, and the
    symptom would appear somewhere unrelated.
    """
    original = redis_module._shared
    yield
    redis_module._shared = original


async def test_one_client_serves_every_consumer(restore_shared: None) -> None:
    """Built once, then handed to everyone.

    A second client is a second connection pool, its own reconnect behaviour, and its
    own view of whether Redis is up — three clients sharing one server and no shared
    answer. The limiter is the interesting consumer to check, because it accepts a
    client by injection for testability: constructed with none, it must resolve to this
    same object rather than quietly building its own.

    Identity, not reachability. Nothing here proves Redis is up, and that is the point:
    if this needed a running server it would be testing the server, not the sharing.
    """
    first = get_client()

    assert isinstance(first, Redis)
    assert get_client() is first
    assert RateLimiter().client is first


async def test_closing_releases_the_client_and_is_idempotent(restore_shared: None) -> None:
    """The shutdown hook's contract.

    Two calls must both succeed: shutdown runs on a clean exit and on a failure, and
    releasing an already-released pool must not raise.

    The stub is substituted rather than closed for real so the *effect* is assertable —
    the pool was released and the global was cleared — rather than merely the absence of
    an exception.
    """
    releases: list[str] = []

    class StubClient:
        async def aclose(self) -> None:
            releases.append("closed")

    redis_module._shared = StubClient()  # type: ignore[assignment]

    await close_client()
    assert releases == ["closed"]
    assert redis_module._shared is None, "the global outlived the pool it pointed at"

    await close_client()
    assert releases == ["closed"], "closing twice released the pool twice"


async def test_a_caller_after_shutdown_gets_a_working_client(restore_shared: None) -> None:
    """Clearing the global is as load-bearing as closing the pool.

    Without it, `get_client()` after shutdown hands back a client whose pool is already
    closed, and the failure surfaces later and elsewhere as a connection error that
    looks nothing like a shutdown-ordering problem. Asserted as identity: the caller must
    get a *different* object, freshly built.
    """
    redis_module._shared = None
    released = get_client()
    await close_client()

    rebuilt = get_client()

    assert rebuilt is not released
    assert redis_module._shared is rebuilt
