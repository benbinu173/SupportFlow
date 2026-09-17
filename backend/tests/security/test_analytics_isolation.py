"""Analytics across two tenants, and across two roles inside one.

Phase S's aggregates are the first reads in this application that summarize *many* rows
without ever naming one, and that changes what a leak looks like. A row-scope bug on
`GET /tickets` hands back a ticket somebody should not see; the same bug here hands back a
**number** — a total that is too large, a risk list with a stranger's ticket at the top —
and nothing on the response says which rows it counted. There is no id to check against,
which is why every count below is asserted against a fixture whose size each tenant chose
for itself.

**Two boundaries, and they fail differently.** The first is the tenant predicate, which
every aggregate inherits from `TenantScopedRepository._select` and which the count tests
below would notice going missing. The second is **row scope inside one tenant**, and it is
the one with no cross-tenant test to catch it: an agent and a manager in the same
organization ask the same route the same question and must get different answers. That
boundary is asserted twice — once through the numbers, and once by looking in Redis and
showing the two callers are held under two different keys. The second is the one that
matters, because it is what stops the first caller's entry being replayed to the second.
"""

from collections.abc import Callable
from typing import Any, cast

import pytest
import redis as redis_sync
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from tests.conftest import API, TICKETS, OrgSession

pytestmark = pytest.mark.security

ANALYTICS = f"{API}/analytics"

# The five routes, so a sixth arriving without a row-scope test is a missing entry here
# rather than an omission nobody sees.
PATHS = ("/overview", "/tickets", "/agents", "/sla", "/sentiment")


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the other isolation files do."""
    # Nothing to do in the body: `truncate_tables` yields, so depending on it is what
    # places the truncation on the far side of the test.


@pytest.fixture
def northwind(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Northwind")


@pytest.fixture
def southwind(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Southwind")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def read(session: OrgSession, path: str) -> dict[str, Any]:
    response = session.get(f"{ANALYTICS}{path}")
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def tickets_seen(body: dict[str, Any], path: str) -> int:
    """How many tickets a route's answer is a summary of.

    One extractor per route so the parametrized tests below can ask the same question of
    five different response shapes — "how much of this tenant did you count" — and get a
    number that is comparable. `/agents` sums the rows' `assigned` rather than using its
    own `total`, because `total` counts *rows* (agents and the unassigned bucket), which is
    one for every tenant and would therefore distinguish nothing.
    """
    if path == "/overview":
        return int(body["totals"]["total"])
    if path == "/tickets":
        return sum(row["count"] for row in body["by_priority"])
    if path == "/agents":
        return sum(row["assigned"] for row in body["agents"])
    if path == "/sla":
        return int(body["open_tickets"])
    return int(body["unanalysed"])


def organization_of(engine: Engine, user_id: str) -> str:
    """The tenant a session belongs to, read from the database.

    Nothing in the API's own responses carries an organization id — the identity is derived
    from the token and never echoed — so a test that wants to look up a cache key has to
    ask the table. `test_log_hygiene.py` does the same for the same reason.
    """
    with engine.begin() as conn:
        value = conn.execute(
            text("SELECT organization_id FROM users WHERE id = CAST(:id AS uuid)"),
            {"id": user_id},
        ).scalar_one()
    return str(value)


def analytics_keys_under(prefix: str) -> set[str]:
    """Every analytics key this Redis holds under `prefix`, decoded.

    A real client against the real server, because the property under test is what the
    application *actually stored* — a fake would assert what the test believes the key
    format is, which is the thing `tests/unit/test_analytics_cache.py` already covers.
    """
    client = redis_sync.Redis.from_url(str(get_settings().REDIS_URL))
    try:
        keys = cast("list[bytes]", client.keys(f"{prefix}*"))
    finally:
        client.close()
    return {key.decode() for key in keys}


def raise_ticket(session: OrgSession, customer_id: str, *, priority: str = "medium") -> dict:
    return session.add_ticket(customer_id, priority=priority)


def age(engine: Engine, ticket_id: Any, *, minutes: int) -> None:
    """Move a ticket's creation into the past, which is the only way to age a clock.

    A second copy of `tests/api/test_analytics.py`'s helper rather than an import: the two
    files are asserting different properties, and an import would make one suite's fixture
    changes a breaking change for the other. The statement is identical for the same reason
    — `make_interval` keeps the count an integer parameter rather than a formatted literal.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tickets SET created_at = created_at - make_interval(mins => :minutes) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(ticket_id), "minutes": minutes},
        )


# ---------------------------------------------------------------------------
# Across tenants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
def test_each_tenant_counts_only_its_own_tickets(
    northwind: OrgSession, southwind: OrgSession, path: str
) -> None:
    """Two tickets here, five there, and neither total is seven.

    The counts differ on purpose: two equal tenants could pass this test while both
    reporting the sum, which is the failure a same-size fixture would hide.
    """
    northwind_customer = northwind.add_customer(name="Northwind's")["id"]
    southwind_customer = southwind.add_customer(name="Southwind's")["id"]
    for _ in range(2):
        raise_ticket(northwind, cast("str", northwind_customer))
    for _ in range(5):
        raise_ticket(southwind, cast("str", southwind_customer))

    assert tickets_seen(read(northwind, path), path) == 2
    assert tickets_seen(read(southwind, path), path) == 5


def test_a_risk_list_never_names_the_other_tenants_tickets(
    northwind: OrgSession, southwind: OrgSession, sync_engine: Engine
) -> None:
    """Identity, not just a count.

    Southwind's ticket is the older of the two, so it would rank first on Northwind's
    dashboard if the tenant predicate were missing — and it is asserted by id rather than
    by position, because a list that happened to be sorted the same way would pass a
    length check.
    """
    northwind_customer = northwind.add_customer(name="Northwind's")["id"]
    southwind_customer = southwind.add_customer(name="Southwind's")["id"]
    theirs = raise_ticket(southwind, cast("str", southwind_customer), priority="urgent")
    ours = raise_ticket(northwind, cast("str", northwind_customer), priority="urgent")
    age(sync_engine, theirs["id"], minutes=600)
    age(sync_engine, ours["id"], minutes=30)

    northwind_risks = [risk["id"] for risk in read(northwind, "/sla")["risks"]]
    southwind_risks = [risk["id"] for risk in read(southwind, "/sla")["risks"]]

    assert northwind_risks == [ours["id"]]
    assert theirs["id"] not in northwind_risks
    assert southwind_risks == [theirs["id"]]


# ---------------------------------------------------------------------------
# Across roles inside one tenant
# ---------------------------------------------------------------------------


def test_an_agent_never_counts_a_colleagues_ticket(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """Two agents in one tenant, one ticket each.

    Both hold `ANALYTICS_OWN`, both call the same route, and the number they get is the
    number of tickets assigned to *them*. A missing row-scope predicate would show each of
    them their colleague's work — a leak inside a tenant, which no cross-tenant assertion
    anywhere in this suite would catch.
    """
    ana = northwind.add_user("agent", name="Ana")
    ben = northwind.add_user("agent", name="Ben")
    customer = northwind.add_customer(name="Northwind's")["id"]
    ticket = raise_ticket(northwind, cast("str", customer))
    assigned = northwind.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": ana.user_id}
    )
    assert assigned.status_code == 200, assigned.text

    ana_sees = read(ana, "/overview")["totals"]
    ben_sees = read(ben, "/overview")["totals"]
    manager_sees = read(northwind, "/overview")["totals"]

    assert ana_sees["total"] == 1
    assert ben_sees["total"] == 0
    # And the tenant-wide reading is neither of theirs — it is everybody's, which is what
    # makes the two above a scope rather than a mistake about which tenant this is.
    assert manager_sees["total"] == 1


def test_two_roles_in_one_tenant_are_cached_under_two_keys(
    northwind: OrgSession, sync_engine: Engine
) -> None:
    """**The phase's second isolation boundary, and the one with no natural test.**

    A cache keyed on `(tenant, metric, range)` alone would be perfectly correct in every
    cross-tenant test in this suite and would still serve an administrator's organization-wide
    total to an agent who asked the same route in the same second. The key therefore carries
    a scope token, and this test asserts the consequence three ways:

    1. the second caller's number is their own, not the first's;
    2. Redis holds an entry under the organization token; and
    3. it holds a *different* entry under the agent's own token.

    Read in that order deliberately: the manager reads first and caches, so an entry
    without the scope segment would already be there when the agent asks.
    """
    agent = northwind.add_user("agent", name="Ana")
    customer = northwind.add_customer(name="Northwind's")["id"]
    theirs = raise_ticket(northwind, cast("str", customer))
    mine = raise_ticket(northwind, cast("str", customer))
    assigned = northwind.post(
        f"{TICKETS}/{mine['id']}/assign", json={"assigned_agent_id": agent.user_id}
    )
    assert assigned.status_code == 200, assigned.text

    organization = organization_of(sync_engine, northwind.user_id)
    manager_total = read(northwind, "/overview")["totals"]["total"]
    agent_total = read(agent, "/overview")["totals"]["total"]

    assert manager_total == 2
    assert agent_total == 1
    assert theirs["id"] not in str(read(agent, "/overview"))

    keys = analytics_keys_under(f"analytics:overview:{organization}:")
    assert any(":org:" in key for key in keys), keys
    assert any(f":user:{agent.user_id}:" in key for key in keys), keys


def test_a_portal_caller_is_refused_before_a_key_is_ever_built(northwind: OrgSession) -> None:
    """A customer holds no analytics capability at all, so the row scope never runs.

    Named as a separate test from the parametrized refusals in `tests/api/test_analytics.py`
    because of what it says about the *cache*: a 403 raised by the route's dependency happens
    before the service is reached, so no key is constructed and no entry is written under a
    customer's scope. The `customer:` branch in `cache.scope_token` is therefore unreachable
    today — asserted here rather than left as a comment somebody later decides is dead code,
    and asserted across the whole keyspace so it cannot be satisfied by a wrong org id.
    """
    customer = northwind.add_customer(name="Grace")["id"]
    portal = northwind.add_portal_user(cast("str", customer))

    for path in PATHS:
        response = portal.get(f"{ANALYTICS}{path}")
        assert response.status_code == 403, f"{path}: {response.text}"

    assert [key for key in analytics_keys_under("analytics:") if ":customer:" in key] == []


def test_the_agent_breakdown_is_refused_to_an_agent_and_open_to_an_admin(
    northwind: OrgSession,
) -> None:
    """§3's line between the two analytics capabilities, at the route that draws it.

    Comparing agents against each other is org-wide analytics; an agent's own performance is
    what the other four routes already return for them. Both halves are asserted, because a
    route that refused everybody would satisfy the first half alone.
    """
    agent = northwind.add_user("agent", name="Ana")

    assert agent.get(f"{ANALYTICS}/agents").status_code == 403
    assert northwind.get(f"{ANALYTICS}/agents").status_code == 200
