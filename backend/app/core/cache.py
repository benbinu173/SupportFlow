"""Analytics caching — the read-through layer §15 asks for, over the shared Redis client.

§15 names dashboard analytics as its own example of what to cache, and requirements.md §7
states the rule: *"Cache expensive dashboard results in Redis. Invalidate or expire caches
appropriately."* This module is that, and only that — keys, a version integer, and one
read-through helper. It knows nothing about analytics; a second namespace could use it
unchanged.

Four decisions worth reading before changing anything here
----------------------------------------------------------

**1. The key carries the row scope, and that is an isolation boundary.** Analytics are
tenant-scoped *and* row-scoped: the same endpoint answers an admin with the organization's
totals and an agent with their own. A key of `(organization, metric, range)` would hand the
agent the admin's payload — a leak *inside* one tenant, which no cross-tenant test would
ever catch, because both callers are in the same organization and the query that filled the
entry was correctly scoped when it ran. So the key embeds `scope_token(context)`: `org` for
a caller whose scope is the whole organization, `user:{id}` for one whose scope is their
assigned work. Two callers with different reach cannot share an entry by construction.
`tests/security/test_analytics_isolation.py` asserts exactly that.

**2. Invalidation is a version integer, not a key sweep.** Redis has no way to delete a
pattern of keys without `SCAN` — and `KEYS`, the obvious substitute, blocks the server and
is banned in production. So a write does not chase the entries it invalidated: it `INCR`s
`analytics:version:{organization_id}`, and every key embeds that number, so an incremented
version makes every existing entry unreachable at once. The cost is that the old entries are
**orphaned rather than deleted** — they sit until their TTL expires. Bounded by the TTL, one
`INCR` per write, and no scanning: the trade ADR-026 records.

**The version key deliberately has no expiry.** A version that could expire would reset to
zero, and a `...:0:...` key written after the reset could collide with a version-0 entry
that had not yet expired — resurrecting a stale payload. Left without a TTL, the version
only ever moves forward. It is one small integer per tenant.

**3. Every path fails open.** Like `realtime.publish` and `notification_service.enqueue_delivery`,
a Redis outage means "compute it", never a failed request — §15's cache is an optimization,
and an optimization that can take a dashboard down is a regression. `invalidate` failing
open means the version does not move and a stale entry can outlive the outage by up to one
TTL; that is stated in the README rather than hidden.

**4. Only the error's *type* is logged, never its message.** A `ConnectionError`'s message
embeds the address it failed to reach, and `REDIS_URL` carries a password in production —
the same reasoning `app/core/redis.py` records for not logging at the point of failure.
`app/websocket/manager.py` logs an unreadable broker message the same way.

**The cached payload is validated on the way back out.** `redis` is not a trusted store: a
value could have been written by a deploy with a different response shape, by a hand-run
`redis-cli`, or by an older version of this process. A payload that does not parse is
treated as a **miss** and recomputed — the same stance the websocket manager takes on a
message that cannot be decoded, and what makes a deploy that changes a response shape safe
instead of a source of `500`s.

Because the value stored is the response model's own JSON and the caller re-serializes that
same model, a cached response and a fresh one are byte-identical. The cache cannot change a
response body; it can only change how long it took.
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.permissions import TICKET_SCOPE_BY_ROLE, RowScope
from app.core.redis import get_client
from app.core.tenancy import TenantContext

logger = structlog.get_logger(__name__)

#: Prefix for every key this module owns, so the cache is greppable in `redis-cli` and so
#: nothing else's keys can be mistaken for its entries.
_PREFIX = "analytics"

#: How many hex characters of the params digest go into a key. 16 characters of a sha256 is
#: 64 bits: two different windows colliding in one tenant's one-minute TTL is not a thing
#: that happens, and the full digest would make the key three times longer than the useful
#: part of it.
_DIGEST_LENGTH = 16


def scope_token(context: TenantContext) -> str:
    """The row scope a cached entry belongs to, as a key segment.

    Derived from `TICKET_SCOPE_BY_ROLE` — the same map the queries narrow by — rather than
    from a role name, so a key cannot disagree with the query that fills it. If an agent's
    scope ever widened to the organization, this token widens with it; that would be a *new*
    token and therefore a miss, which is the safe direction. A token that stayed narrow
    while the query widened would be a leak.

    The `OWN` branch is unreachable today — no role holding an analytics capability resolves
    to it — but it is written out rather than left to fall through to `org`. A fallthrough
    would hand a future portal-facing analytics route the organization's numbers, which is
    precisely the failure this function exists to prevent.
    """
    scope = context.scope_for(TICKET_SCOPE_BY_ROLE)

    if scope is RowScope.ORGANIZATION:
        return "org"
    if scope is RowScope.ASSIGNED:
        # The authenticated user's id, from the token. Never anything from the request.
        return f"user:{context.user_id}"
    return f"customer:{context.customer_id}"


def cache_key(
    namespace: str,
    *,
    organization_id: uuid.UUID,
    scope: str,
    version: int,
    params: Mapping[str, Any],
) -> str:
    """One entry's key: everything that makes two requests the same question.

    Four segments beyond the namespace, and each is there because leaving it out would
    serve one caller another's answer: the **tenant**, the **row scope** (decision 1 above),
    the **version** (decision 2), and a digest of the **parameters**.

    The digest is over `json.dumps(params, sort_keys=True, default=str)`, so the same window
    spelled two ways — a `datetime` and its ISO string, a dict built in a different key
    order — produces one entry rather than two. Truncated, because it is a fingerprint of a
    handful of query parameters and not a security boundary; the scope token beside it is
    what stops a leak, and it is not hashed precisely so it stays readable in `redis-cli`.

    Nothing parses this key back apart. If something ever needs to, the scope token's colon
    is what will make that awkward, and it should be changed to a separator then.
    """
    encoded = json.dumps(params, sort_keys=True, default=str).encode()
    digest = hashlib.sha256(encoded).hexdigest()[:_DIGEST_LENGTH]
    return f"{_PREFIX}:{namespace}:{organization_id}:{scope}:{version}:{digest}"


def _version_key(organization_id: uuid.UUID) -> str:
    """The invalidation counter for one tenant. Never expires — see decision 2."""
    return f"{_PREFIX}:version:{organization_id}"


async def get_version(organization_id: uuid.UUID) -> int:
    """The tenant's current version, or `0` if Redis has never been written to.

    `0` is the only sensible default: it is what a tenant's first request sees before any
    write has happened, and it means the same thing as a counter that has never been
    incremented. Nothing writes the key at `0` — the first `invalidate` creates it at `1`.
    """
    try:
        raw = await get_client().get(_version_key(organization_id))
    except (RedisError, OSError) as exc:
        # OSError as well as RedisError: a refused connection surfaces from the socket
        # layer before redis-py wraps it. `rate_limit.py` records the same pairing.
        logger.warning(
            "analytics_version_unavailable",
            organization_id=str(organization_id),
            error_type=type(exc).__name__,
        )
        return 0

    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        # The version key is Redis data, and Redis data is untrusted input. A value that
        # is not an integer means something else wrote here; treating it as "no version
        # yet" costs a cache miss and cannot serve a stale entry.
        logger.warning("analytics_version_unreadable", organization_id=str(organization_id))
        return 0


async def invalidate(organization_id: uuid.UUID, *, client: Redis | None = None) -> None:
    """Move the tenant's version forward, making every existing entry unreachable.

    Called from the ticket writers, beside the realtime publish and in the same post-commit
    position — the aggregate that just changed is the aggregate these entries describe.

    Fails open. A write during a Redis outage leaves the version where it was, so entries
    written before the outage stay readable until their TTL runs out. That is a *stale
    number* rather than a lost write, and it is bounded by the TTL; the alternative is
    failing the write itself, which would be a data-loss bug in exchange for a fresher
    dashboard.

    **`client` is for a caller that does not own a long-lived event loop.** `get_client`'s
    own docstring is explicit that the shared client is correct only in the API, and
    `app/workers/ai_tasks.py` reaches this through `event_loop.run`, which builds *and
    closes* a loop per invocation — so the task passes the `redis.scoped_client()` it is
    already holding for its publish, exactly as `realtime.publish` lets it. The default
    keeps every request-path call site unchanged and correct.
    """
    try:
        await (client or get_client()).incr(_version_key(organization_id))
    except (RedisError, OSError) as exc:
        logger.warning(
            "analytics_invalidate_failed",
            organization_id=str(organization_id),
            error_type=type(exc).__name__,
            detail="the cached aggregate may be stale until its TTL expires",
        )


async def key_for(namespace: str, context: TenantContext, params: Mapping[str, Any]) -> str:
    """The key for one request: the namespace, the caller, and the window.

    The ergonomic entry point, and the reason `cache_key` is not called directly anywhere.
    Building a key requires the current version and the caller's scope token, and a call
    site that assembled those itself could forget one — a key without the scope token is
    the in-tenant leak decision 1 exists to prevent. Here there is nothing to forget.
    """
    organization_id = context.organization_id
    return cache_key(
        namespace,
        organization_id=organization_id,
        scope=scope_token(context),
        version=await get_version(organization_id),
        params=params,
    )


async def read_through[ModelT: BaseModel](
    key: str,
    *,
    model: type[ModelT],
    ttl: int,
    produce: Callable[[], Awaitable[ModelT]],
) -> ModelT:
    """Return the entry at `key`, computing and storing it if it is not there.

    The read, the miss, and the write are one call by design: a caller that could read
    without the fallback would eventually do so and return `None` to a route that promised
    a response.

    Three ways to miss, and they are deliberately not distinguished at the call site — a
    miss is a miss and all three are answered by `produce`: no such key, Redis unreachable,
    and a stored value that does not parse as `model`. The last is the interesting one; it
    is a **miss rather than an error** so that a deploy changing a response shape cannot
    turn yesterday's entries into `500`s.

    `produce` is called only on a miss, so a cached response costs one `GET` and no queries.
    """
    cached = await _read(key, model=model)
    if cached is not None:
        return cached

    value = await produce()
    await _write(key, value, ttl=ttl)
    return value


async def _read[ModelT: BaseModel](key: str, *, model: type[ModelT]) -> ModelT | None:
    """The entry at `key`, or `None` for any reason at all."""
    try:
        raw = await get_client().get(key)
    except (RedisError, OSError) as exc:
        _unavailable("get", exc)
        return None

    if raw is None:
        return None

    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        # Not an error path: the caller recomputes and overwrites. Logged at warning
        # because it is a signal worth seeing — it means a response shape changed under
        # entries that are still alive.
        logger.warning("analytics_cache_unreadable", key=key, error_type=type(exc).__name__)
        return None


async def _write(key: str, value: BaseModel, *, ttl: int) -> None:
    """Store one entry with an expiry. Every entry has one; see decision 2."""
    try:
        await get_client().set(key, value.model_dump_json(), ex=ttl)
    except (RedisError, OSError) as exc:
        _unavailable("set", exc)


def _unavailable(operation: str, exc: Exception) -> None:
    """One log line for every way Redis can be absent, shared by both paths.

    A helper rather than two copies because the two call sites must log the same event with
    the same fields — a cache that is down should look the same whether the failure was
    noticed on the read or on the write.
    """
    logger.warning(
        "analytics_cache_unavailable",
        operation=operation,
        error_type=type(exc).__name__,
        detail="computing the response without caching",
    )
