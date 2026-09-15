"""Rate limiting — Redis fixed-window counters.

Guards the endpoints where guessing is the attack: login (credential stuffing),
registration (account-farm creation), and upload (one account filling the bucket).
Spec §45 requires the controls, §46 requires the 429 path be tested.

**This limiter fails open, deliberately.** If Redis is unreachable the request is
allowed and a warning is logged. Rate limiting is an abuse control, not an
authentication control: failing closed would turn a Redis blip into a total login
outage for every user of every tenant, which is a far worse outcome than a few minutes
of unthrottled login attempts. The trade-off is recorded in ADR-014 so it stays a
decision rather than becoming an accident.

No HTTP types here. `app/api/deps.py` derives the key from the request; this module
only knows about strings and counters, which is what lets it be tested without a
client.

The Redis client itself lives in `app/core/redis.py` — it is shared with the readiness
probe, and a second consumer is the point at which it stops belonging to this module.
"""

import uuid

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.exceptions import RateLimitedError
from app.core.redis import get_client

logger = structlog.get_logger(__name__)


class RateLimiter:
    """Fixed-window counter over a Redis client.

    Takes its client rather than reaching for the shared one, so a test can supply a
    stand-in and exercise the decision logic — including the Redis-down path — without
    a running server.
    """

    def __init__(self, client: Redis | None = None) -> None:
        self._client = client

    @property
    def client(self) -> Redis:
        if self._client is None:
            self._client = get_client()
        return self._client

    async def enforce(self, key: str, *, limit: int, window_seconds: int) -> None:
        """Count one request against `key`; raise `RateLimitedError` if over `limit`.

        The window starts at the first request and does not slide: a caller who hits
        the limit waits out the remainder of a fixed window rather than being locked
        out indefinitely by a steady trickle of requests.
        """
        try:
            count, retry_after = await self._hit(key, window_seconds)
        except (RedisError, OSError) as exc:
            # OSError as well as RedisError: a refused connection surfaces from the
            # socket layer before redis-py wraps it, and "Redis is down" is exactly
            # the case this branch exists for.
            logger.warning(
                "rate_limit_unavailable",
                key=key,
                error=str(exc),
                detail="allowing the request; the limiter fails open by design",
            )
            return

        if count > limit:
            logger.info("rate_limited", key=key, count=count, limit=limit)
            raise RateLimitedError(retry_after=retry_after)

    async def _hit(self, key: str, window_seconds: int) -> tuple[int, int]:
        """Increment the window and return `(count, seconds_until_reset)`.

        Pipelined into one round trip. `SET ... NX EX` is the only command that can
        create the key, and it always sets an expiry — so the counter can never be
        left without a TTL. That matters: a key created by a bare `INCR` and then
        orphaned by a crash between it and the `EXPIRE` would lock the caller out
        permanently, since nothing would ever clear it.
        """
        client = self.client
        async with client.pipeline(transaction=True) as pipe:
            pipe.set(key, 0, ex=window_seconds, nx=True)
            pipe.incr(key)
            pipe.ttl(key)
            results = await pipe.execute()

        count = int(results[1])
        ttl = int(results[2])
        # -1 means "exists with no expiry" and -2 "no such key"; neither is reachable
        # given how the key is created, so fall back to the full window rather than
        # reporting a nonsense Retry-After.
        return count, ttl if ttl > 0 else window_seconds


def login_rate_limit_key(ip_address: str) -> str:
    """Redis key for the login limiter, per client IP.

    Per IP because at this point there is no identity yet — the caller has not
    authenticated, so the address is the only thing to count against (ADR-014).
    """
    return f"ratelimit:login:{ip_address}"


def register_rate_limit_key(ip_address: str) -> str:
    """Redis key for the registration limiter, per client IP."""
    return f"ratelimit:register:{ip_address}"


def upload_rate_limit_key(user_id: uuid.UUID) -> str:
    """Redis key for the upload limiter, per *user*.

    The opposite key to the two above, and deliberately so. Upload is authenticated, so
    identity is available and is the precise thing to count: §45's concern is one
    account filling the bucket. Keying it on the address would reproduce cost #2 that
    ADR-014 already records against the login limiter — one person's backlog throttling
    everyone behind the same NAT — and this is the one place where that trade is
    avoidable rather than forced, because the identity exists.
    """
    return f"ratelimit:upload:{user_id}"
