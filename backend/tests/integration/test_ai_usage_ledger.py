"""The ledger, written for real: what a call costs, who it is charged to, and who can see it.

**This is the file where `ai_usage` stops being an empty table.** Phase D created it,
Phase S built the aggregate that reads it and wrote down that every number was zero
*"because nothing writes `ai_usage` yet"*, and the shape of `AIUsageSummary` has been final
since then. `tests/unit/test_ai_retry.py` proves the *staging* — that each attempt queues a
row with the right fields — against a recording stand-in for the session. What a stand-in
cannot show is that the row survives a commit, satisfies the table's constraints, and is
**found by the aggregate that already existed**. That needs a real database, and it is the
only claim this file makes.

**Why the service layer and not the route.** There is no AI endpoint: §36's three
(`/ai/analyze`, `/ai/summarize`, `/ai/suggest-response`) belong to Phases U, V, and W, and
Phase T adds no route at all. So the shipped read path under test is
`analytics_service.overview`, and the numbers are asserted where they are computed rather
than through HTTP. `tests/api/test_analytics.py` already covers the route's own behaviour —
the window, the status code, the response shape — and
`scripts/phase_t_walkthrough.py` is what proves the two halves meet over a socket.

**No `truncate_tables`, and no HTTP.** Every row is written through the async `db` fixture,
whose outer transaction is rolled back on teardown, following
`tests/integration/test_analytics_sla_agreement.py`. The rows are built through the ORM
because `ai_usage.created_at` is a server default and the tenant has to exist before a call
can be attributed to one; a tenant registered over HTTP would work, and would also make this
file depend on the registration rate limit for no gain.

**`analytics_service.overview` is cached, so this file reads it at most once per tenant per
window.** A second read of the same key answers from Redis — or recomputes, if Redis is
down — so an assertion about what a write changed would pass or fail depending on whether a
container is running, which is the worst kind of test. Every other number here is read
through the repository the service calls, which has no cache in front of it; `overview` is
read exactly once, against a key nothing has written yet, for the claim that the shipped
path reports the ledger at all.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.errors import AIOutputError, AITransientError
from app.ai.fake import FakeProvider
from app.ai.pricing import cost_usd
from app.ai.provider import AIRequest
from app.core import cache
from app.core.config import get_settings
from app.core.exceptions import AIServiceError
from app.core.tenancy import TenantContext
from app.models.ai_usage import AIUsage
from app.models.customer import Customer
from app.models.enums import AIOperation, UserRole
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.user import User
from app.repositories.analytics_repository import AnalyticsRepository
from app.services import ai_service, analytics_service

pytestmark = pytest.mark.integration

# A placeholder, not a credential: nothing in this file authenticates. Same constant, and
# the same reasoning, as `tests/security/test_row_scopes.py`.
NOT_A_CREDENTIAL = "unused-in-this-test"

#: `FakeProvider`'s defaults, restated so an assertion that fails says which number moved.
PROMPT_TOKENS = 1_200
COMPLETION_TOKENS = 80

#: What that costs at Sonnet 5's published $2/$10 per MTok — 0.0024 in, 0.0008 out.
EXPECTED_COST = Decimal("0.003200")


def window(hours: int = 1) -> tuple[datetime, datetime]:
    """A window around now, wide enough to contain anything this file writes.

    The ledger row's `created_at` is the database's clock, so the end is in the future
    rather than at `now()`: a window that ended at the instant the test read the clock
    would exclude a row written a microsecond later, and the assertion would be about
    which came first rather than about whether the row exists.
    """
    now = datetime.now(UTC)
    return now - timedelta(hours=hours), now + timedelta(hours=hours)


def past_window() -> tuple[datetime, datetime]:
    """A window that closed an hour ago — the range a report about last week would ask for.

    Spelled as its own function rather than as `window(hours=-1)`, which would be an
    inverted range that happens to select nothing. A test whose window is `start > end` is
    testing the query planner, not the window.
    """
    now = datetime.now(UTC)
    return now - timedelta(hours=3), now - timedelta(hours=2)


def request() -> AIRequest:
    """A request with customer-written text in it, as every real one has."""
    return AIRequest(
        instruction="Classify the ticket.",
        content="I was charged twice for order 88213.",
        content_label="the customer's message",
        max_tokens=1024,
    )


def classify_payload() -> dict[str, Any]:
    return {"category": "Billing", "subcategory": "Duplicate Charge", "confidence": 0.94}


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> FakeProvider:
    """Point the service at a scripted provider.

    `ai_service._provider` is the seam, and it is the only thing here that is replaced —
    everything below it, including the row that reaches the table, is the real path.
    """
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


@pytest.fixture
async def ledgers(db: AsyncSession) -> dict[str, Any]:
    """Two tenants, each with a customer, an admin, and one ticket.

    Two because the isolation claim needs a tenant that spent nothing to compare against,
    and a single-tenant fixture would satisfy every aggregate assertion here while the
    tenant predicate was missing. Modelled on `tests/security/test_row_scopes.py`'s
    `scope_rows`, which gives the same reason for building two.
    """
    tenants: dict[str, dict[str, Any]] = {}

    for key in ("spender", "bystander"):
        suffix = uuid.uuid4().hex[:8]
        organization = Organization(name=f"{key.title()} Co", slug=f"{key}-{suffix}")
        db.add(organization)
        await db.flush()

        customer = Customer(
            organization_id=organization.id,
            name=f"{key.title()} Customer",
            email=f"customer@{key}-{suffix}.example",
        )
        user = User(
            organization_id=organization.id,
            name=f"{key.title()} Admin",
            email=f"admin@{key}-{suffix}.example",
            password_hash=NOT_A_CREDENTIAL,
            role=UserRole.ADMIN,
        )
        db.add_all([customer, user])
        await db.flush()

        ticket = Ticket(
            organization_id=organization.id,
            number=1,
            customer_id=customer.id,
            subject="Charged twice",
            description="Order 88213 was billed twice.",
        )
        db.add(ticket)
        await db.flush()

        tenants[key] = {
            "organization": organization,
            "customer": customer,
            "user": user,
            "ticket": ticket,
            "context": TenantContext(
                user_id=user.id,
                organization_id=organization.id,
                role=UserRole.ADMIN,
            ),
        }

    return tenants


@pytest.fixture
async def loop_local_cache(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Point the analytics cache at a Redis client *this test's* event loop owns.

    **This is a property of the suite, not a workaround for a defect in the cache.**
    `app/core/redis.py` caches one client for the process and says so in its own docstring:
    it is *"correct only in a process that owns one long-lived event loop"*. The test suite
    is not such a process. `client` in `tests/conftest.py` is session-scoped and runs the
    ASGI app on a loop in a worker thread, so by the time an integration test runs, the
    shared client has usually been built on **that** loop — and a `redis.asyncio` connection
    captures the loop that opened it. An async test reusing it reads through a connection
    whose loop belongs to somebody else, which asyncio refuses with *"running two read
    coroutines at the same time"* — an error that has nothing to do with what is under test.

    Only this file's read of `overview` meets it, because it is the first async test in the
    suite to use the cache in-process; every earlier cache assertion goes through HTTP, where
    the request runs on the same worker loop that built the client. This test would pass
    alone and fail in the full suite without the fixture, which is precisely the kind of
    failure that gets misread as flakiness.

    So the client is built here, on the loop this test is running on, and closed with it —
    the same thing `scoped_client()` exists for in a Celery task, and what
    `tests/integration/test_sla_sweep.py` does for its pub/sub connection.
    """
    client = Redis.from_url(str(get_settings().REDIS_URL))
    monkeypatch.setattr(cache, "get_client", lambda: client)
    try:
        yield
    finally:
        await client.aclose()


@pytest.fixture
def spender(ledgers: dict[str, Any]) -> dict[str, Any]:
    return ledgers["spender"]


@pytest.fixture
def bystander(ledgers: dict[str, Any]) -> dict[str, Any]:
    return ledgers["bystander"]


async def ledger_rows(db: AsyncSession, organization_id: uuid.UUID) -> list[AIUsage]:
    """Every row the tenant has, read back the way the table stores it."""
    result = await db.execute(
        select(AIUsage)
        .where(AIUsage.organization_id == organization_id)
        .order_by(AIUsage.operation)
    )
    return list(result.scalars().all())


async def aggregate(db: AsyncSession, tenant: dict[str, Any], *, hours: int = 1) -> dict[str, Any]:
    """The Phase S aggregate, called directly — no cache in the path, no window ambiguity."""
    start, end = window(hours=hours)
    repository = AnalyticsRepository(db, tenant["context"])
    return await repository.ai_usage(start, end)


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------


async def test_a_committed_call_leaves_a_row_in_the_table(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim the whole phase rests on: `ai_usage` is an empty table no longer.

    A `SELECT` is the assertion, not the object the service passed to `add`. The row has to
    survive a commit, satisfy three `CheckConstraint`s and six `NOT NULL`s, and come back
    as the same numbers — none of which a recording session can show.
    """
    use_provider(monkeypatch, FakeProvider(classify_payload()))

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()

    stored = await ledger_rows(db, spender["organization"].id)

    assert len(stored) == 1
    row = stored[0]
    assert row.operation is AIOperation.CLASSIFY
    assert row.provider == "fake"
    assert row.model == get_settings().AI_MODEL
    assert row.prompt_tokens == PROMPT_TOKENS
    assert row.completion_tokens == COMPLETION_TOKENS
    assert row.cost_usd == EXPECTED_COST
    assert row.was_successful is True
    assert row.was_cached is False


async def test_the_row_is_attributed_to_the_caller_and_the_ticket(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§28's per-user attribution and the ticket join, both of which the aggregate needs.

    `user_id` is what makes abuse investigation possible and `ticket_id` is what the row
    scope travels through, so a row missing either is a row the dashboard cannot place.
    """
    use_provider(monkeypatch, FakeProvider(classify_payload()))

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()

    row = (await ledger_rows(db, spender["organization"].id))[0]

    assert row.organization_id == spender["organization"].id
    assert row.user_id == spender["user"].id
    assert row.ticket_id == spender["ticket"].id
    assert isinstance(row.latency_ms, int)
    assert row.latency_ms >= 0
    assert row.created_at is not None


async def test_a_failed_call_is_recorded_and_priced_like_a_successful_one(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated answer is still an invoice.

    The client gets one stable `AIServiceError`, and the ledger gets the call: the tokens
    say it was generated, `was_successful` says it bought nothing usable, and `cost_usd` is
    what was actually charged. Dropping the row would report spend with no way to see how
    much of it was wasted, which is what `failed_calls` exists for.
    """
    use_provider(
        monkeypatch,
        FakeProvider(
            AIOutputError(
                "the answer was truncated at max_tokens",
                prompt_tokens=900,
                completion_tokens=40,
            )
        ),
    )

    with pytest.raises(AIServiceError):
        await ai_service.classify_ticket(
            db, spender["context"], request(), ticket_id=spender["ticket"].id
        )
    # The obligation `ai_service`'s docstring states out loud: the caller commits, and it
    # commits on the failure path too. A ledger row that vanished with the caller's rollback
    # would be missing exactly when it matters.
    await db.commit()

    stored = await ledger_rows(db, spender["organization"].id)

    assert len(stored) == 1
    assert stored[0].was_successful is False
    assert (stored[0].prompt_tokens, stored[0].completion_tokens) == (900, 40)
    assert stored[0].cost_usd == cost_usd(
        get_settings().AI_MODEL, prompt_tokens=900, completion_tokens=40
    )
    assert stored[0].cost_usd > 0


async def test_every_attempt_of_a_retried_call_is_its_own_row(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rules at once, and they interact: retries are bounded, and none is free.

    `AI_MAX_ATTEMPTS` bounds how many calls a request can make, and this is the row count
    that shows the bound being reached through the real path — a transient failure followed
    by a success is two rows and one returned answer.
    """
    use_provider(
        monkeypatch,
        FakeProvider(
            AITransientError("the provider could not be reached"),
            classify_payload(),
        ),
    )

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()

    stored = await ledger_rows(db, spender["organization"].id)

    assert len(stored) == 2
    assert sorted(row.was_successful for row in stored) == [False, True]


async def test_all_four_operations_reach_the_ledger_with_their_own_name(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§17's four generation methods, each writing the row `by_operation` is grouped by.

    `tests/unit/test_ai_retry.py` proves this against a stand-in session. Here it is the
    real table with the real enum column: a value that the PostgreSQL type does not accept
    is a `DataError` in production and nothing at all in a fake.
    """
    use_provider(
        monkeypatch,
        FakeProvider(
            classify_payload(),
            {"sentiment": "negative", "confidence": 0.96},
            {"summary": "The customer was billed twice and wants a refund."},
            {"body": "Thanks for flagging this — I have refunded the duplicate charge."},
        ),
    )
    context, ticket_id = spender["context"], spender["ticket"].id
    payload = request()

    await ai_service.classify_ticket(db, context, payload, ticket_id=ticket_id)
    await ai_service.analyze_sentiment(db, context, payload, ticket_id=ticket_id)
    await ai_service.summarize_conversation(db, context, payload, ticket_id=ticket_id)
    await ai_service.generate_response(db, context, payload, ticket_id=ticket_id)
    await db.commit()

    stored = await ledger_rows(db, spender["organization"].id)

    assert {row.operation for row in stored} == {
        AIOperation.CLASSIFY,
        AIOperation.SENTIMENT,
        AIOperation.SUMMARIZE,
        AIOperation.SUGGEST_RESPONSE,
    }
    assert all(row.was_successful for row in stored)


# ---------------------------------------------------------------------------
# The aggregate Phase S built and left at zero
# ---------------------------------------------------------------------------


async def test_the_spend_appears_in_the_aggregate(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Totals, and the per-operation breakdown the dashboard's second row is built from."""
    use_provider(monkeypatch, FakeProvider(classify_payload()))

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()

    summary = await aggregate(db, spender)

    assert summary["calls"] == 1
    assert summary["failed_calls"] == 0
    assert summary["prompt_tokens"] == PROMPT_TOKENS
    assert summary["completion_tokens"] == COMPLETION_TOKENS
    assert summary["cost_usd"] == EXPECTED_COST
    assert {row["operation"]: row["calls"] for row in summary["by_operation"]} == {
        AIOperation.CLASSIFY: 1
    }


async def test_the_breakdown_separates_the_operations_by_what_they_cost(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two operations, two rows, each carrying its own spend rather than a shared total.

    Distinct rates per operation are what makes "which feature is expensive" answerable,
    which is the index `ix_ai_usage_org_operation_created` exists for.
    """
    use_provider(
        monkeypatch,
        FakeProvider(
            classify_payload(),
            prompt_tokens=0,
            completion_tokens=0,
        ),
    )
    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    monkeypatch.setattr(
        ai_service,
        "_provider",
        lambda: FakeProvider(
            {"sentiment": "negative", "confidence": 0.9},
            prompt_tokens=4_000,
            completion_tokens=1_000,
        ),
    )
    await ai_service.analyze_sentiment(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()

    summary = await aggregate(db, spender)
    by_operation = {row["operation"]: row for row in summary["by_operation"]}

    assert summary["calls"] == 2
    assert by_operation[AIOperation.CLASSIFY]["cost_usd"] == Decimal("0")
    assert by_operation[AIOperation.SENTIMENT]["cost_usd"] == cost_usd(
        get_settings().AI_MODEL, prompt_tokens=4_000, completion_tokens=1_000
    )
    # The two sum to the total, which is the property that would break first if the
    # breakdown were grouped by something other than the same window and row scope.
    assert summary["cost_usd"] == sum(
        (row["cost_usd"] for row in summary["by_operation"]), Decimal(0)
    )


async def test_the_window_excludes_calls_made_outside_it(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` and `end` are the caller's, and a call outside them is not in the answer.

    Phase S's window resolution has its own tests; what is checked here is only that the
    ledger is subject to it. A spend report for last week must not move when today's call
    arrives, and the way to know that is to ask for last week.
    """
    use_provider(monkeypatch, FakeProvider(classify_payload()))

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()

    # A window that closed before the call: the row exists, and this range does not see it.
    closed = await aggregate(db, spender, hours=-1)

    assert closed["calls"] == 0
    assert closed["cost_usd"] == Decimal("0")
    assert closed["by_operation"] == []
    assert len(await ledger_rows(db, spender["organization"].id)) == 1


async def test_the_overview_route_reports_the_ledger_it_used_to_read_empty(
    db: AsyncSession,
    spender: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    loop_local_cache: None,
) -> None:
    """The shipped read path, called once — see the module docstring on the cache.

    `AIUsageSummary`'s docstring said every field was zero *"because nothing writes
    `ai_usage` yet"*. This is the assertion that stops being a tautology.
    """
    use_provider(monkeypatch, FakeProvider(classify_payload()))

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()

    start, end = window()
    overview = await analytics_service.overview(db, spender["context"], start=start, end=end)

    assert overview.ai_usage.calls == 1
    assert overview.ai_usage.failed_calls == 0
    assert overview.ai_usage.prompt_tokens == PROMPT_TOKENS
    assert overview.ai_usage.cost_usd == EXPECTED_COST
    assert [row.operation for row in overview.ai_usage.by_operation] == [AIOperation.CLASSIFY]


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


async def test_a_tenant_that_has_spent_nothing_reads_zeros(
    db: AsyncSession,
    spender: dict[str, Any],
    bystander: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4's tenant isolation, at the only place it could leak: a shared ledger table.

    Both tenants have a customer, an admin, and a ticket, and only one has a call. A
    missing `organization_id` predicate in the aggregate would report the spender's spend
    to the bystander — and would look perfectly correct in a single-tenant suite, which is
    why the fixture builds two.
    """
    use_provider(monkeypatch, FakeProvider(classify_payload()))

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()

    theirs = await aggregate(db, bystander)

    assert theirs["calls"] == 0
    assert theirs["failed_calls"] == 0
    assert theirs["prompt_tokens"] == 0
    assert theirs["completion_tokens"] == 0
    assert theirs["cost_usd"] == Decimal("0")
    assert theirs["by_operation"] == []
    # And of course nothing was charged to them.
    assert await ledger_rows(db, bystander["organization"].id) == []
    # The spender still sees it, so the zeros above are the predicate working and not the
    # write having failed.
    assert (await aggregate(db, spender))["calls"] == 1


async def test_the_row_scope_is_what_admits_a_call_to_the_totals(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call with no ticket is a real row that no row-scoped view can see.

    `analytics_repository.ai_usage` scopes through the *ticket*, because `ai_usage` has no
    `customer_id` and no `assigned_agent_id` — a call's audience is the ticket it was made
    for. Embedding calls during ingestion belong to no ticket and are therefore outside
    every row-scoped view, which the repository's docstring states. This is that sentence
    as an assertion, and it is deliberately not "the row is dropped": the ledger keeps it,
    because cost attribution and ingestion's own spend report need every call.
    """
    use_provider(monkeypatch, FakeProvider(classify_payload()))

    # No `ticket_id`: the shape an ingestion-time call has.
    await ai_service.classify_ticket(db, spender["context"], request())
    await db.commit()

    assert len(await ledger_rows(db, spender["organization"].id)) == 1
    assert (await aggregate(db, spender))["calls"] == 0


async def test_a_call_is_not_visible_to_another_tenants_ticket(
    db: AsyncSession,
    ledgers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is charged to the caller's tenant whatever ticket it names.

    `ticket_id` is passed in by the caller, so it is the one field here a caller could get
    wrong. Both halves are asserted: the tenant is the context's — §4's *"never trust an
    `organization_id` supplied by the frontend"*, which `_stage_usage` enforces by taking a
    `TenantContext` rather than an id — and the bystander's aggregate is unmoved by a row
    pointing at their ticket.
    """
    spender, bystander = ledgers["spender"], ledgers["bystander"]
    use_provider(monkeypatch, FakeProvider(classify_payload()))

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=bystander["ticket"].id
    )
    await db.commit()

    stored = await ledger_rows(db, spender["organization"].id)
    assert len(stored) == 1
    assert stored[0].organization_id == spender["organization"].id
    assert await ledger_rows(db, bystander["organization"].id) == []
    assert (await aggregate(db, bystander))["calls"] == 0


# ---------------------------------------------------------------------------
# The aggregate's own arithmetic, on the rows this file just made
# ---------------------------------------------------------------------------


async def test_the_totals_are_the_sum_of_the_rows(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SUM` over `Numeric` in PostgreSQL, against Python's own `Decimal` sum of the rows.

    Six decimal places of a fraction of a cent is the quantity floating point starts losing
    once the sums get large, so the comparison is exact — the same assertion
    `tests/unit/test_ai_pricing.py` makes about the arithmetic, made here about the column.
    """
    use_provider(
        monkeypatch,
        FakeProvider(
            classify_payload(),
            classify_payload(),
            classify_payload(),
            prompt_tokens=1_337,
            completion_tokens=421,
        ),
    )
    for _ in range(3):
        await ai_service.classify_ticket(
            db, spender["context"], request(), ticket_id=spender["ticket"].id
        )
    await db.commit()

    stored = await ledger_rows(db, spender["organization"].id)
    summary = await aggregate(db, spender)

    assert summary["calls"] == len(stored) == 3
    assert summary["prompt_tokens"] == sum(row.prompt_tokens for row in stored)
    assert summary["completion_tokens"] == sum(row.completion_tokens for row in stored)
    assert summary["cost_usd"] == sum((row.cost_usd for row in stored), Decimal(0))
    assert summary["cost_usd"] == 3 * cost_usd(
        get_settings().AI_MODEL, prompt_tokens=1_337, completion_tokens=421
    )


async def test_a_failed_call_counts_toward_the_total_it_bought_nothing_with(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`failed_calls` is a subset of `calls`, not a separate bucket.

    The distinction is the whole reason the two are reported side by side, and it is easy
    to get wrong in the other direction — a `COUNT(*) FILTER` that excluded failures from
    `SUM(cost_usd)` would show a tenant a total that was not what they were billed.
    """
    use_provider(
        monkeypatch,
        FakeProvider(
            AIOutputError(
                "the answer was not the requested schema",
                prompt_tokens=500,
                completion_tokens=10,
            )
        ),
    )

    with pytest.raises(AIServiceError):
        await ai_service.classify_ticket(
            db, spender["context"], request(), ticket_id=spender["ticket"].id
        )
    await db.commit()

    summary = await aggregate(db, spender)

    assert summary["calls"] == 1
    assert summary["failed_calls"] == 1
    assert summary["cost_usd"] == cost_usd(
        get_settings().AI_MODEL, prompt_tokens=500, completion_tokens=10
    )
    assert summary["cost_usd"] > 0


async def test_the_ledger_is_append_only(
    db: AsyncSession, spender: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second call adds a row; nothing rewrites the first.

    `AIUsage` is the record of what was charged, and a historical row that could be updated
    would stop being one the moment a rate changed. Asserted as a row count and as the
    first row's fields, since `test_the_row_is_attributed_to_the_caller_and_the_ticket`
    already pins what is in it.
    """
    use_provider(monkeypatch, FakeProvider(classify_payload(), classify_payload()))

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()
    first = (await ledger_rows(db, spender["organization"].id))[0]
    first_id, first_created = first.id, first.created_at

    await ai_service.classify_ticket(
        db, spender["context"], request(), ticket_id=spender["ticket"].id
    )
    await db.commit()

    stored = await ledger_rows(db, spender["organization"].id)

    assert len(stored) == 2
    assert {row.id for row in stored} >= {first_id}
    assert await db.scalar(select(func.count()).select_from(AIUsage)) == 2
    again = next(row for row in stored if row.id == first_id)
    assert again.created_at == first_created
    assert again.was_successful is True
