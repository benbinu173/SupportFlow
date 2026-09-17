"""The analytics cache's own decisions, without a database or a Redis server.

Everything asserted here is a property of `app/core/cache.py` in isolation, which is why
it lives in `tests/unit`: the root conftest is explicit that this package has to run with
nothing else running, so Redis is a fake object and the "Redis is down" cases are raised
exceptions rather than a stopped container. The live version of that — the endpoint still
answering with the server actually stopped — is a step in
`scripts/phase_s_walkthrough.py`, because it can only be done once.

**Three of these tests are isolation tests wearing a unit-test hat.** The key's scope
token, its tenant, and its version are the three segments that stop one caller being
served another's numbers, and each is asserted by constructing two keys and showing they
differ. A key that lost any of the three would still work perfectly in a single-tenant,
single-role suite — which is exactly the failure mode
`tests/security/test_analytics_isolation.py` covers from the other end, over HTTP.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core import cache
from app.core.tenancy import TenantContext
from app.models.enums import UserRole
from app.schemas.analytics import SLAComplianceSummary, SLAStanding, SLATimerCompliance
from app.schemas.sla import SLATimer

pytestmark = pytest.mark.unit

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
WINDOW = {"start": datetime(2026, 1, 1, tzinfo=UTC), "end": datetime(2026, 2, 1, tzinfo=UTC)}


class FakeRedis:
    """The four commands this module uses, in a dict.

    Deliberately not a mock: `read_through` has to be observed *not* calling `produce` a
    second time, and a mock that answered `None` to every `get` could not distinguish a
    hit from a miss. A dict can.
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes | str] = {}
        self.ttls: dict[str, int | None] = {}
        self.gets = 0
        self.sets = 0

    async def get(self, key: str) -> bytes | str | None:
        self.gets += 1
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.sets += 1
        self.ttls[key] = ex
        self.store[key] = value

    async def incr(self, key: str) -> int:
        current = int(self.store.get(key) or 0)
        self.store[key] = str(current + 1)
        return current + 1


class UnreachableRedis:
    """A client that refuses every command, the way a stopped server does.

    `ConnectionError` rather than a bare `Exception`: the module catches `(RedisError,
    OSError)`, and a real refused connection arrives as an `OSError` subclass from the
    socket layer before redis-py wraps it — `rate_limit.py` records the same pairing. A
    fake raising something outside that set would prove the cache handles a failure it
    would never actually see.
    """

    async def get(self, key: str) -> None:
        raise ConnectionError("connection refused")

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        raise ConnectionError("connection refused")

    async def incr(self, key: str) -> int:
        raise ConnectionError("connection refused")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(cache, "get_client", lambda: client)
    return client


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "get_client", lambda: UnreachableRedis())


def context(
    *, organization_id: uuid.UUID = ORG, role: UserRole = UserRole.MANAGER
) -> TenantContext:
    """A caller in `organization_id` whose role decides their row scope.

    A manager by default because a manager's ticket scope is the whole organization —
    so the default context produces the `org` token, and the agent variant below is what
    the scope tests differ against.
    """
    return TenantContext(user_id=uuid.uuid4(), organization_id=organization_id, role=role)


def standing() -> SLAStanding:
    """A small instance of a real cached model, so `read_through` is exercised on the
    thing it actually caches rather than on a stand-in schema."""
    response = SLATimerCompliance(timer=SLATimer.RESPONSE, met=3, breached=1, rate=0.75)
    resolution = SLATimerCompliance(timer=SLATimer.RESOLUTION, met=2, breached=0, rate=1.0)

    return SLAStanding(
        compliance=SLAComplianceSummary(
            response=response,
            resolution=resolution,
            met=5,
            breached=1,
            rate=5 / 6,
        ),
        overdue=1,
        open_tickets=4,
    )


async def produce_standing() -> SLAStanding:
    """`standing` in the shape `read_through` takes, which is an awaitable factory."""
    return standing()


def key(**overrides: Any) -> str:
    """`cache_key` with everything defaulted, so each test overrides only what it is about."""
    arguments: dict[str, Any] = {
        "namespace": "overview",
        "organization_id": ORG,
        "scope": "org",
        "version": 0,
        "params": WINDOW,
    }
    arguments.update(overrides)
    return cache.cache_key(**arguments)


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def test_the_same_window_spelled_two_ways_produces_one_key() -> None:
    """A dict built in a different order digests the same, because `sort_keys` is set.

    Without it, `{"start", "end"}` and `{"end", "start"}` — which is what two call sites
    written on different days produce — would be two entries for one question, and the
    cache would be half as effective for no reason anyone could see.
    """
    forwards = key(params={"start": WINDOW["start"], "end": WINDOW["end"]})
    backwards = key(params={"end": WINDOW["end"], "start": WINDOW["start"]})

    assert forwards == backwards


def test_a_datetime_and_its_string_form_produce_one_key() -> None:
    """`default=str` means a timestamp digests the same whether it arrived as a
    `datetime` or as the ISO string a cache layer handed on."""
    assert key(params={"start": WINDOW["start"]}) == key(params={"start": str(WINDOW["start"])})


def test_a_different_window_produces_a_different_key() -> None:
    """The control for the two above: the digest does distinguish what it should."""
    assert key(params=WINDOW) != key(params={**WINDOW, "end": datetime(2026, 3, 1, tzinfo=UTC)})


def test_a_different_tenant_produces_a_different_key() -> None:
    """The first isolation segment. Two organizations asking one question never share."""
    assert key() != key(organization_id=OTHER_ORG)


def test_a_different_row_scope_produces_a_different_key() -> None:
    """**The second isolation segment, and the one no cross-tenant test would catch.**

    An admin and an agent in the same organization, the same window, the same namespace:
    the same question with different answers, because one reaches the whole organization
    and the other reaches only their own assignments. A key without this segment would
    serve whichever of them asked first to both, and every cross-tenant assertion in the
    suite would still pass.
    """
    assert key(scope="org") != key(scope=f"user:{uuid.uuid4()}")


def test_a_different_version_produces_a_different_key() -> None:
    """The third: invalidation makes old entries unreachable by changing the key."""
    assert key(version=7) != key(version=8)


# ---------------------------------------------------------------------------
# The scope token
# ---------------------------------------------------------------------------


def test_a_manager_and_an_admin_share_the_organization_token() -> None:
    """Both reach every row in the tenant, so both may share one entry.

    Asserted positively rather than left implicit: if this returned a per-role token the
    cache would still be correct and half as useful, and nothing else would notice.
    """
    assert cache.scope_token(context(role=UserRole.ADMIN)) == "org"
    assert cache.scope_token(context(role=UserRole.MANAGER)) == "org"


def test_an_agent_gets_a_token_naming_them() -> None:
    """An agent's reach is their own assignments, so their entry is their own."""
    agent = context(role=UserRole.AGENT)

    assert cache.scope_token(agent) == f"user:{agent.user_id}"


def test_two_agents_in_one_organization_get_different_tokens() -> None:
    """Two agents see different tickets, so one must never be served the other's entry."""
    first = context(role=UserRole.AGENT)
    second = context(role=UserRole.AGENT)

    assert cache.scope_token(first) != cache.scope_token(second)


def test_a_portal_caller_gets_a_token_of_their_own() -> None:
    """Unreachable today — no role with an analytics capability resolves to `OWN` — and
    asserted anyway, because the branch that matters is the one that does *not* fall
    through to `org`. A fallthrough would hand a portal caller the organization's numbers
    the day an analytics route is opened to them."""
    customer_id = uuid.uuid4()
    portal = TenantContext(
        user_id=uuid.uuid4(),
        organization_id=ORG,
        role=UserRole.CUSTOMER,
        customer_id=customer_id,
    )

    token = cache.scope_token(portal)
    assert token == f"customer:{customer_id}"
    assert token != "org"


# ---------------------------------------------------------------------------
# The version integer
# ---------------------------------------------------------------------------


async def test_the_version_starts_at_zero(fake: FakeRedis) -> None:
    """Nothing writes the key at zero — the first invalidation creates it at one — so a
    tenant that has never changed a ticket reads a version that was never stored."""
    assert await cache.get_version(ORG) == 0
    assert fake.store == {}


async def test_invalidate_moves_the_version_forward(fake: FakeRedis) -> None:
    """Two invalidations take it to 2, and the key holds no expiry.

    The absence of a TTL is the point: a version that could expire would reset to zero,
    and a key written at version 0 afterwards could collide with a version-0 entry that
    had not expired yet — resurrecting a stale payload. Asserted here because it is the
    kind of thing a later "add a TTL to everything" tidy-up would remove.
    """
    await cache.invalidate(ORG)
    await cache.invalidate(ORG)

    assert await cache.get_version(ORG) == 2
    assert cache._version_key(ORG) in fake.store
    assert cache._version_key(ORG) not in fake.ttls


async def test_invalidation_is_per_tenant(fake: FakeRedis) -> None:
    """One organization's write does not invalidate another's entries."""
    await cache.invalidate(ORG)

    assert await cache.get_version(ORG) == 1
    assert await cache.get_version(OTHER_ORG) == 0


async def test_a_version_that_is_not_a_number_reads_as_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis data is untrusted input. A value something else wrote is a cache miss, not
    a `ValueError` on the way to a `500`."""
    client = FakeRedis()
    client.store[cache._version_key(ORG)] = b"not-a-number"
    monkeypatch.setattr(cache, "get_client", lambda: client)

    assert await cache.get_version(ORG) == 0


async def test_the_version_is_read_from_redis_on_every_request(fake: FakeRedis) -> None:
    """`key_for` consults the counter rather than caching it in the process.

    A process-local copy would be wrong the moment a second API instance invalidated —
    which is the whole reason the version lives in Redis and not in a module global.
    """
    first = await cache.key_for("overview", context(), WINDOW)
    await cache.invalidate(ORG)
    second = await cache.key_for("overview", context(), WINDOW)

    assert first != second


# ---------------------------------------------------------------------------
# read_through
# ---------------------------------------------------------------------------


async def test_a_miss_computes_and_stores(fake: FakeRedis) -> None:
    calls = 0

    async def produce() -> SLAStanding:
        nonlocal calls
        calls += 1
        return standing()

    result = await cache.read_through("key", model=SLAStanding, ttl=300, produce=produce)

    assert result == standing()
    assert calls == 1
    assert fake.store["key"] == standing().model_dump_json()
    assert fake.ttls["key"] == 300


async def test_a_hit_does_not_compute(fake: FakeRedis) -> None:
    """The cached bytes round-trip into an equal model, and `produce` is not called.

    This is the assertion the whole module exists for: one `GET` and no queries.
    """
    fake.store["key"] = standing().model_dump_json()

    async def produce() -> SLAStanding:
        raise AssertionError("produce must not run on a hit")

    assert (
        await cache.read_through("key", model=SLAStanding, ttl=300, produce=produce) == standing()
    )


async def test_a_payload_that_does_not_parse_is_recomputed(fake: FakeRedis) -> None:
    """**A miss, not an error.** A stored value from an older response shape must not be
    able to turn into a `500` — it is recomputed and overwritten, which is also what makes
    a deploy that changes a schema safe."""
    fake.store["key"] = '{"compliance": "this is not what it was"}'

    result = await cache.read_through("key", model=SLAStanding, ttl=300, produce=produce_standing)

    assert result == standing()
    assert fake.store["key"] == standing().model_dump_json()


async def test_redis_being_down_is_a_miss(offline: None) -> None:
    """Fails open: a Redis outage means "compute it", never a failed request."""
    assert (
        await cache.read_through("key", model=SLAStanding, ttl=300, produce=produce_standing)
        == standing()
    )


async def test_a_write_failure_does_not_lose_the_response(offline: None) -> None:
    """The value is returned even though it could not be stored.

    Worth asserting separately from the read path: a cache that returned the value on a
    read failure but raised on a write failure would be broken in the one state it is
    most likely to be in — Redis going down while the app is serving.
    """
    result = await cache.read_through("key", model=SLAStanding, ttl=300, produce=produce_standing)

    assert result.overdue == 1


async def test_invalidate_being_unable_to_reach_redis_does_not_raise(offline: None) -> None:
    """Every path fails open, including this one. The cost is stated rather than hidden:
    the version does not move, so an entry written before the outage stays readable until
    its TTL expires."""
    await cache.invalidate(ORG)


async def test_get_version_being_unable_to_reach_redis_reads_as_zero(offline: None) -> None:
    """And the consequence is a *miss* rather than a stale hit: version 0 is a key no
    entry was written under unless a real version 0 wrote one, which cannot happen after
    the first write."""
    assert await cache.get_version(ORG) == 0
