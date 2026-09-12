"""The rate limiter's decision logic, against a stand-in Redis.

No server, no HTTP. The limiter takes its client by injection precisely so this is
possible: the counting arithmetic and the Redis-down path are the parts that matter,
and neither needs a network to exercise.

The stand-in models real Redis semantics closely enough that the tests are about our
code rather than about the double — in particular `INCR` on a missing key creates one
*without* an expiry, which is the hazard `app/core/rate_limit.py` is built to avoid.
"""

import math
from typing import Any
from unittest.mock import MagicMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from app.core import rate_limit
from app.core.exceptions import ErrorCode, RateLimitedError
from app.core.rate_limit import (
    RateLimiter,
    _shared_client,
    close_client,
    login_rate_limit_key,
    register_rate_limit_key,
)

pytestmark = pytest.mark.unit

WINDOW = 60
LIMIT = 3


class FakePipeline:
    """Records commands, then applies them in order — as a Redis transaction does.

    Commands are buffered and applied on `execute`, so a test can inspect exactly what
    the limiter asked for. That matters for the TTL guarantee: the correctness of the
    window depends on *which* command creates the key, and that is only observable at
    this level.
    """

    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.ops: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def set(self, key: str, value: Any, *, ex: int | None = None, nx: bool = False) -> None:
        self.ops.append(("set", key, value, ex, nx))

    def incr(self, key: str) -> None:
        self.ops.append(("incr", key))

    def ttl(self, key: str) -> None:
        self.ops.append(("ttl", key))

    async def execute(self) -> list[Any]:
        if self.redis.execute_failure is not None:
            raise self.redis.execute_failure
        return [self._apply(op) for op in self.ops]

    def _apply(self, op: tuple[Any, ...]) -> Any:
        store = self.redis.store
        now = self.redis.now

        if op[0] == "set":
            _, key, value, ex, nx = op
            if nx and self.redis._live(key) is not None:
                # `SET key v EX n NX` is a no-op when the key exists — this is what
                # makes it safe to issue on every request.
                return None
            store[key] = (value, None if ex is None else now + ex)
            return True

        if op[0] == "incr":
            key = op[1]
            current = self.redis._live(key)
            # Matching Redis: incrementing a missing key creates it with **no** TTL.
            if current is None:
                store[key] = (1, None)
                return 1
            store[key] = (current + 1, store[key][1])
            return current + 1

        key = op[1]
        if key not in store:
            return -2  # no such key
        _, expires_at = store[key]
        if expires_at is None:
            return -1  # exists, but immortal
        return max(0, math.ceil(expires_at - now))


class FakeRedis:
    """Enough of `redis.asyncio.Redis` for the limiter, and no more."""

    def __init__(
        self,
        *,
        pipeline_failure: Exception | None = None,
        execute_failure: Exception | None = None,
    ) -> None:
        self.store: dict[str, tuple[int, float | None]] = {}
        self.now: float = 1_000.0
        self.pipeline_failure = pipeline_failure
        self.execute_failure = execute_failure
        self.pipelines: list[FakePipeline] = []

    def _live(self, key: str) -> int | None:
        """The key's value, or `None` if absent or expired."""
        entry = self.store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and self.now >= expires_at:
            del self.store[key]
            return None
        return value

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        assert transaction, "the limiter relies on a transactional pipeline"
        if self.pipeline_failure is not None:
            raise self.pipeline_failure
        pipe = FakePipeline(self)
        self.pipelines.append(pipe)
        return pipe

    def advance(self, seconds: float) -> None:
        """Move the clock forward, as the passage of a window would."""
        self.now += seconds

    @property
    def commands(self) -> list[str]:
        """Every command issued across every pipeline, in order."""
        return [op[0] for pipe in self.pipelines for op in pipe.ops]


def limiter_for(redis: FakeRedis) -> RateLimiter:
    return RateLimiter(client=redis)  # type: ignore[arg-type]


async def exhaust(limiter: RateLimiter, key: str, times: int) -> None:
    for _ in range(times):
        await limiter.enforce(key, limit=LIMIT, window_seconds=WINDOW)


# ---------------------------------------------------------------------------
# The limit is enforced
# ---------------------------------------------------------------------------


async def test_requests_within_the_limit_are_allowed() -> None:
    """A limit of N permits N requests, not N-1.

    The off-by-one is worth pinning: `count > limit` and `count >= limit` differ by
    exactly one attempt, and only one of them matches what "10 per minute" means.
    """
    redis = FakeRedis()
    limiter = limiter_for(redis)

    await exhaust(limiter, "k", LIMIT)  # does not raise


async def test_the_request_that_exceeds_the_limit_is_rejected() -> None:
    redis = FakeRedis()
    limiter = limiter_for(redis)
    await exhaust(limiter, "k", LIMIT)

    with pytest.raises(RateLimitedError) as raised:
        await limiter.enforce("k", limit=LIMIT, window_seconds=WINDOW)

    assert raised.value.code is ErrorCode.RATE_LIMITED
    assert raised.value.status_code == 429
    assert raised.value.retry_after is not None
    assert 0 < raised.value.retry_after <= WINDOW


async def test_the_rejection_reports_a_usable_retry_after() -> None:
    """The header a client waits on must be the window's real remainder.

    A `Retry-After` of 0 would invite an immediate retry that is guaranteed to fail,
    and one larger than the window would make the client wait for nothing.
    """
    redis = FakeRedis()
    limiter = limiter_for(redis)
    await exhaust(limiter, "k", LIMIT)

    redis.advance(20)
    with pytest.raises(RateLimitedError) as raised:
        await limiter.enforce("k", limit=LIMIT, window_seconds=WINDOW)

    assert raised.value.retry_after == 40
    assert raised.value.headers == {"Retry-After": "40"}


async def test_the_count_is_kept_per_key() -> None:
    """One caller exhausting their budget must not throttle anyone else.

    The key is per-endpoint and per-IP (`login_rate_limit_key`), so this is what makes
    the limiter a limit on *a client* rather than on the application.
    """
    redis = FakeRedis()
    limiter = limiter_for(redis)

    await exhaust(limiter, login_rate_limit_key("10.0.0.1"), LIMIT)

    # A different address is unaffected...
    await exhaust(limiter, login_rate_limit_key("10.0.0.2"), LIMIT)
    # ...and so is the other endpoint, for the same address.
    await exhaust(limiter, register_rate_limit_key("10.0.0.1"), LIMIT)


async def test_the_counter_clears_when_the_window_passes() -> None:
    """Fixed window, and it does end — a limiter that never forgot would lock a
    legitimate user out of their own account permanently."""
    redis = FakeRedis()
    limiter = limiter_for(redis)
    await exhaust(limiter, "k", LIMIT)

    with pytest.raises(RateLimitedError):
        await limiter.enforce("k", limit=LIMIT, window_seconds=WINDOW)

    redis.advance(WINDOW)

    await exhaust(limiter, "k", LIMIT)  # a fresh window, full budget


# ---------------------------------------------------------------------------
# The window cannot leak
# ---------------------------------------------------------------------------


async def test_the_creating_command_sets_an_expiry() -> None:
    """The failure this design exists to prevent.

    A bare `INCR` on a missing key creates one with **no** TTL. If that were the only
    command, a key orphaned before its `EXPIRE` would never clear and the caller would
    be locked out until someone deleted it by hand. `SET ... NX EX` is the only
    command issued that can create the key, and it always sets the expiry.

    Asserted on the command sequence rather than on the resulting TTL, because the
    ordering is the guarantee — the TTL being present is a consequence.
    """
    redis = FakeRedis()
    limiter = limiter_for(redis)

    await limiter.enforce("k", limit=LIMIT, window_seconds=WINDOW)

    assert redis.commands == ["set", "incr", "ttl"]

    (_, key, value, ex, nx) = redis.pipelines[0].ops[0]
    assert key == "k"
    assert value == 0
    assert ex == WINDOW
    assert nx is True


async def test_every_key_that_exists_has_a_positive_ttl() -> None:
    """The invariant, checked after a realistic sequence rather than after one call."""
    redis = FakeRedis()
    limiter = limiter_for(redis)

    for _ in range(3):
        for key in (login_rate_limit_key("10.0.0.1"), register_rate_limit_key("10.0.0.9")):
            await limiter.enforce(key, limit=10, window_seconds=WINDOW)

    assert redis.store
    for key, (_, expires_at) in redis.store.items():
        assert expires_at is not None, f"{key} was created without an expiry"
        assert expires_at > redis.now


async def test_the_key_is_not_recreated_by_later_requests() -> None:
    """`NX` means only the first request in a window sets the expiry.

    Without it, every request would push the deadline forward and a steady trickle
    would keep the window open forever — the count would never reset, which turns a
    per-minute limit into a permanent ban.
    """
    redis = FakeRedis()
    limiter = limiter_for(redis)

    await limiter.enforce("k", limit=LIMIT, window_seconds=WINDOW)
    first_deadline = redis.store["k"][1]

    redis.advance(30)
    await limiter.enforce("k", limit=LIMIT, window_seconds=WINDOW)

    assert redis.store["k"][1] == first_deadline


# ---------------------------------------------------------------------------
# Failing open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        RedisConnectionError("connection refused"),
        RedisError("something else went wrong"),
        # Not a RedisError at all. A refused socket surfaces from the transport
        # before redis-py wraps it, which is exactly when this branch matters most.
        OSError("connection refused"),
    ],
    ids=["connection-error", "redis-error", "os-error"],
)
async def test_an_unreachable_redis_allows_the_request(failure: Exception) -> None:
    """Deliberate, and the reasoning is recorded in ADR-014.

    Rate limiting is an abuse control, not an authentication control. Failing closed
    would convert a Redis outage into a total login outage for every user of every
    tenant — strictly worse than a few minutes of unthrottled attempts.
    """
    limiter = limiter_for(FakeRedis(pipeline_failure=failure))

    await exhaust(limiter, "k", LIMIT * 10)  # never raises


async def test_a_failure_mid_execution_also_allows_the_request() -> None:
    """The pipeline context manager can open and the command still fail.

    Covered separately because the two failures surface from different calls, and only
    one of them is inside the `try` if the exception handling is placed wrongly.
    """
    limiter = limiter_for(FakeRedis(execute_failure=RedisConnectionError("gone")))

    await exhaust(limiter, "k", LIMIT * 10)  # never raises


async def test_the_open_failure_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-open is only defensible while it is audible.

    If Redis goes down and nothing says so, the limiter silently stops protecting the
    login endpoint and nobody finds out. The logger is replaced rather than the output
    captured, because structlog resolves its output stream once and a captured-stdout
    assertion would depend on which test logged first.
    """
    logged = MagicMock()
    monkeypatch.setattr("app.core.rate_limit.logger", logged)

    limiter = limiter_for(FakeRedis(pipeline_failure=RedisConnectionError("refused")))
    await limiter.enforce("k", limit=LIMIT, window_seconds=WINDOW)

    logged.warning.assert_called_once()
    event, fields = logged.warning.call_args
    assert event[0] == "rate_limit_unavailable"
    assert fields["key"] == "k"
    assert "refused" in fields["error"]


async def test_a_rejection_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refusals are recorded too — a spike is the evidence that the limit is doing
    its job, and the count is what distinguishes an attack from a stuck client."""
    logged = MagicMock()
    monkeypatch.setattr("app.core.rate_limit.logger", logged)

    redis = FakeRedis()
    limiter = limiter_for(redis)
    await exhaust(limiter, "k", LIMIT)

    with pytest.raises(RateLimitedError):
        await limiter.enforce("k", limit=LIMIT, window_seconds=WINDOW)

    logged.info.assert_called_once()
    event, fields = logged.info.call_args
    assert event[0] == "rate_limited"
    assert fields["count"] == LIMIT + 1
    assert fields["limit"] == LIMIT


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_keys_are_endpoint_scoped_and_readable() -> None:
    """Two endpoints must not share a counter, and a key must be identifiable in
    `redis-cli` during an incident."""
    assert login_rate_limit_key("203.0.113.7") == "ratelimit:login:203.0.113.7"
    assert register_rate_limit_key("203.0.113.7") == "ratelimit:register:203.0.113.7"
    assert login_rate_limit_key("1.1.1.1") != register_rate_limit_key("1.1.1.1")


def test_the_shared_client_is_built_once() -> None:
    """Lazily built, then process-wide.

    `redis.asyncio` clients own a connection pool, so constructing one per login would
    open and discard a connection every time. Resolving the URL opens no connection,
    which is why this needs no running Redis.

    **Deliberately does not close it.** In a full run this module executes after the
    HTTP suite, whose application has already built — and used — this same global on its
    own event loop. Releasing a live pool from here would be reaching across loops,
    which is the failure this asserts nothing about. `close_client` is covered
    separately, against a client this test owns.
    """
    first = _shared_client()

    assert isinstance(first, Redis)
    assert _shared_client() is first

    # And a limiter with no client of its own resolves to that same object.
    assert RateLimiter().client is first


async def test_closing_releases_the_shared_client_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Called from the app's shutdown hook, so it has to be both effective and safe.

    The second call is the case that matters: shutdown runs on a clean exit and on a
    failure, and releasing an already-released pool must not raise.

    The global is replaced with a stub rather than closed for real — see the test above
    for why. Substituting it also lets the *effect* be asserted (the pool was released,
    the global was cleared) rather than merely the absence of an exception.
    """
    releases: list[str] = []

    class StubClient:
        async def aclose(self) -> None:
            releases.append("closed")

    monkeypatch.setattr("app.core.rate_limit._shared", StubClient())

    await close_client()
    assert releases == ["closed"]
    assert rate_limit._shared is None

    await close_client()
    assert releases == ["closed"], "closing twice released the pool twice"
