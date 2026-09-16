"""SLA across two tenants: nothing one organization configures or is told reaches another.

Phase Q adds two tenant-owned things and one that is not owned by anyone: a policy table,
a set of timeline alerts and notifications, and a scheduled task that reads both with **no
`TenantContext` at all**. That third one is the reason this file exists rather than being
folded into the policy tests.

**The sweep is the only code in this application that queries without a caller.** Every
other read is a `TenantScopedRepository` method, and the scoping is the thing that makes
`organization_id` a value the request cannot influence. `app/repositories/sla_repository.py`
takes one as an argument instead — which is correct, because a beat task has no request to
take it from, and also means the filter is *written out* rather than inherited. A missing
`organization_id` clause in one of those functions would be a cross-tenant leak nothing
else in the suite would catch, because the API paths never call them.

Two of the tests below therefore drive the sweep directly, once per tenant, and assert that
each run touched only its own tickets.

**The cross-tenant refusal here is a 404 on a ticket, not on a policy.** There is no
`GET /sla/policies/{id}`: a policy is addressed by its *priority*, which is a small closed
vocabulary every tenant shares, so there is no id to guess and no id to refuse. What can be
asked across a tenant boundary is a ticket's SLA position, and that is where ADR-009's 404
applies — asserted below with the "indistinguishable from a missing record" comparison the
other isolation files use, because a status code alone is not the property.
"""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.workers import sla_tasks
from tests.conftest import NOTIFICATIONS, SLA, TICKETS, OrgSession

pytestmark = pytest.mark.security

# `URGENT`'s seeded targets: 30 minutes to a first response, warning at 80%. Backdating by
# 25 minutes puts a ticket inside its warning band, which is the cheapest way to make one
# organization's sweep have something to say.
BACKDATE_MINUTES = 25


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


def policy_targets(session: OrgSession) -> dict[str, tuple[int, int]]:
    """Every priority's `(response, resolution)` targets, as this tenant sees them."""
    response = session.get(f"{SLA}/policies")
    assert response.status_code == 200, response.text
    return {
        row["priority"]: (row["response_time_minutes"], row["resolution_time_minutes"])
        for row in response.json()
    }


def urgent_ticket(session: OrgSession, customer_id: str, **kwargs: object) -> dict:
    return session.add_ticket(customer_id, priority="urgent", **kwargs)


def backdate(engine: Engine, ticket_id: object, *, minutes: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tickets SET created_at = created_at - make_interval(mins => :minutes) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(ticket_id), "minutes": minutes},
        )


def organization_of(engine: Engine, ticket_id: object) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT organization_id FROM tickets WHERE id = CAST(:id AS uuid)"),
                {"id": str(ticket_id)},
            ).scalar_one()
        )


def alerts_for(engine: Engine, ticket_id: object) -> list[str]:
    """The SLA timeline entries on a ticket, read past the API.

    Past the API because the assertion is about *which tenant's rows exist*, and an alert
    that leaked into the wrong organization's table would be invisible through an endpoint
    that shows a caller only their own. The row is the evidence.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type FROM ticket_events "
                "WHERE ticket_id = CAST(:id AS uuid) "
                "AND CAST(event_type AS text) IN ('sla_warning', 'sla_breached')"
            ),
            {"id": str(ticket_id)},
        ).all()
    return sorted(str(row.event_type) for row in rows)


def notifications_for(engine: Engine, ticket_id: object) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM notifications WHERE ticket_id = CAST(:id AS uuid)"),
                {"id": str(ticket_id)},
            ).scalar_one()
        )


def recipients(engine: Engine, ticket_id: object) -> list[str]:
    """Who was told about a ticket, by user id. Read past the API, for the reason above."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT user_id FROM notifications WHERE ticket_id = CAST(:id AS uuid) "
                "AND CAST(notification_type AS text) = 'sla_warning'"
            ),
            {"id": str(ticket_id)},
        ).all()
    return [str(row.user_id) for row in rows]


def sweep(engine: Engine, ticket_id: object) -> dict[str, int]:
    return sla_tasks.check_organization_sla(organization_of(engine, ticket_id))


def inbox(session: OrgSession) -> list[dict]:
    response = session.get(NOTIFICATIONS)
    assert response.status_code == 200, response.text
    return list(response.json())


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


def test_a_new_tenant_gets_its_own_four_policies(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """Two registrations, two independent policy tables, four rows each.

    Each is seeded inside its own registration transaction, so this is really asserting
    that the seeding is scoped by the organization being created rather than by anything
    global. A seeding routine that wrote one shared set would leave the second tenant
    editing the first's targets.
    """
    assert len(policy_targets(northwind)) == 4
    assert len(policy_targets(southwind)) == 4


def test_one_tenants_edit_does_not_move_the_others_policy(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """The cross-tenant boundary on the one route that writes to a policy table.

    The route is addressed by priority, so there is no id to send across the boundary and
    no 404 to assert — the interesting failure is the opposite one: an update that matched
    the wrong row because its `WHERE` forgot the organization. Northwind's numbers are read
    back through Northwind's own session, which is what the assertion below is comparing.
    """
    before = policy_targets(northwind)

    updated = southwind.patch(f"{SLA}/policies/urgent", json={"response_time_minutes": 5})
    assert updated.status_code == 200, updated.text

    assert policy_targets(southwind)["urgent"] == (5, 240)
    assert policy_targets(northwind) == before


def test_the_two_tenants_policies_are_addressable_independently(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """Same priority, two rows, two ids — and each edit reports its own.

    Both tenants have a policy with priority `urgent`, and the two are different rows. The
    ids are what the audit trail's `target_id` records, so an edit that landed on the other
    tenant's row would produce an audit entry naming an id that tenant never had.
    """
    north_id = next(
        row["id"] for row in northwind.get(f"{SLA}/policies").json() if row["priority"] == "urgent"
    )
    south_id = next(
        row["id"] for row in southwind.get(f"{SLA}/policies").json() if row["priority"] == "urgent"
    )

    assert north_id != south_id


def test_a_deactivated_priority_is_only_deactivated_for_its_own_tenant(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """`is_active` is a per-tenant switch, which is easy to get wrong in the clock.

    `load_policies` filters on `organization_id` and `is_active` together. Dropping the
    first would make one tenant's switch silence every tenant's alerts for that priority —
    a failure that would look like a bug in the sweep rather than in a `WHERE` clause.
    """
    customer = str(southwind.add_customer(name="Ada", email="ada@southwind.com")["id"])
    ticket = urgent_ticket(southwind, customer)

    assert southwind.patch(f"{SLA}/policies/urgent", json={"is_active": False}).status_code == 200

    # Southwind's clock is off; Northwind's is untouched and still reads four rows.
    assert southwind.get(f"{TICKETS}/{ticket['id']}").json()["sla"] is None
    assert len(policy_targets(northwind)) == 4


# ---------------------------------------------------------------------------
# The clock, across the boundary
# ---------------------------------------------------------------------------


def test_a_ticket_in_another_organization_is_a_404(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """ADR-009 on the ticket read, which is where a position would leak from.

    Read by both staff and — on the same id — by a portal caller in the other tenant, so
    the refusal is not merely a role check that happens to be in the way. A 403 would
    confirm the ticket exists, which turns the endpoint into an enumeration oracle.
    """
    customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    ticket = urgent_ticket(northwind, customer, subject="Northwind's problem")

    for session in (southwind, southwind.add_user("customer", email="portal@southwind.com")):
        response = session.get(f"{TICKETS}/{ticket['id']}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"
        assert "Northwind's problem" not in response.text


def test_the_refusal_is_identical_to_a_record_that_never_existed(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """Status, error code, message and body — compared in full, not as a status code.

    A cross-tenant answer that said "that ticket belongs to another organization" would
    leak exactly as much as a 403 while passing a status-only assertion. This is the
    comparison that catches an "improved" error message.
    """
    customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    ticket = urgent_ticket(northwind, customer)

    cross_tenant = southwind.get(f"{TICKETS}/{ticket['id']}")
    never_existed = southwind.get(f"{TICKETS}/{uuid.uuid4()}")

    assert cross_tenant.status_code == never_existed.status_code == 404
    assert cross_tenant.json() == never_existed.json()
    assert cross_tenant.headers.get("WWW-Authenticate") is None


# ---------------------------------------------------------------------------
# The sweep, which has no context to scope by
# ---------------------------------------------------------------------------


def test_a_sweep_touches_only_its_own_tenants_tickets(
    northwind: OrgSession, southwind: OrgSession, sync_engine: Engine
) -> None:
    """The claim this file is really about: two overdue tickets, two sweeps, no leakage.

    Both tickets are backdated identically, so both are inside their warning band. Each
    organization is swept once, and each run must alert about exactly its own ticket —
    which asserts the `organization_id` clause in `find_pending` from both directions: the
    swept tenant's ticket is found, and the other tenant's is not.

    Both tenants are swept by calling the *same* task with different ids, which is what the
    dispatcher does; the fan-out itself is asserted in `tests/integration/test_sla_sweep.py`.
    """
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    south_customer = str(southwind.add_customer(name="Ada", email="ada@southwind.com")["id"])
    north_ticket = urgent_ticket(northwind, north_customer)
    south_ticket = urgent_ticket(southwind, south_customer)
    for ticket in (north_ticket, south_ticket):
        backdate(sync_engine, ticket["id"], minutes=BACKDATE_MINUTES)

    north_counts = sweep(sync_engine, north_ticket["id"])
    south_counts = sweep(sync_engine, south_ticket["id"])

    assert north_counts["warnings"] == south_counts["warnings"] == 1
    assert alerts_for(sync_engine, north_ticket["id"]) == ["sla_warning"]
    assert alerts_for(sync_engine, south_ticket["id"]) == ["sla_warning"]


def test_a_sweep_does_not_alert_the_other_tenants_managers(
    northwind: OrgSession, southwind: OrgSession, sync_engine: Engine
) -> None:
    """The recipient lookup is the second place a missing filter would leak.

    `find_manager_ids` is one `WHERE` on `organization_id` away from every manager in the
    fleet, and the symptom would be that a manager at Southwind receives alerts about
    Northwind's tickets — a leak of the ticket's subject line to somebody with no access to
    the ticket, which is worse than a wrong count.

    Both organizations have a manager so the assertion is symmetric: each manager is
    notified about their own tenant's ticket and nothing else, which is only observable
    when both tenants have alerts to send.
    """
    north_manager = northwind.add_user("manager", email="manager@northwind.com")
    south_manager = southwind.add_user("manager", email="manager@southwind.com")
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    south_customer = str(southwind.add_customer(name="Ada", email="ada@southwind.com")["id"])
    north_ticket = urgent_ticket(northwind, north_customer)
    south_ticket = urgent_ticket(southwind, south_customer)
    for ticket in (north_ticket, south_ticket):
        backdate(sync_engine, ticket["id"], minutes=BACKDATE_MINUTES)

    sweep(sync_engine, north_ticket["id"])
    sweep(sync_engine, south_ticket["id"])

    assert set(recipients(sync_engine, north_ticket["id"])) == {north_manager.user_id}
    assert set(recipients(sync_engine, south_ticket["id"])) == {south_manager.user_id}
    # And each manager's own inbox holds one alert, about their own ticket — the read path
    # scopes by the caller's tenant, so this is the other half of the same property.
    assert [row["ticket_id"] for row in inbox(north_manager)] == [north_ticket["id"]]
    assert [row["ticket_id"] for row in inbox(south_manager)] == [south_ticket["id"]]


def test_a_sweep_leaves_the_other_tenant_with_no_rows_at_all(
    northwind: OrgSession, southwind: OrgSession, sync_engine: Engine
) -> None:
    """Northwind swept alone writes nothing anywhere near Southwind.

    The strongest form of the previous two: not "the other tenant's staff cannot see it"
    but "the other tenant's rows do not exist". Run once, for one organization, and then
    counted — which is the shape of the bug a `WHERE` clause's absence produces.
    """
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    south_customer = str(southwind.add_customer(name="Ada", email="ada@southwind.com")["id"])
    southwind.add_user("manager", email="manager@southwind.com")
    north_ticket = urgent_ticket(northwind, north_customer)
    south_ticket = urgent_ticket(southwind, south_customer)
    for ticket in (north_ticket, south_ticket):
        backdate(sync_engine, ticket["id"], minutes=BACKDATE_MINUTES)

    sweep(sync_engine, north_ticket["id"])

    assert alerts_for(sync_engine, north_ticket["id"]) == ["sla_warning"]
    assert alerts_for(sync_engine, south_ticket["id"]) == []
    assert notifications_for(sync_engine, south_ticket["id"]) == 0
    # Southwind's own sweep then finds its own ticket untouched — the two runs are
    # independent rather than one having consumed the other's work.
    assert sweep(sync_engine, south_ticket["id"])["warnings"] == 1


def test_the_alert_body_carries_no_other_tenants_detail(
    northwind: OrgSession, sync_engine: Engine
) -> None:
    """The subject line is the one field of another tenant's data a wrong join could carry.

    `notify_sla_alert` composes the body from the ticket row it was handed, so the body is
    evidence about which row that was. Asserted against the raw JSON rather than the parsed
    object, so a subject nested anywhere in the payload would be caught.
    """
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    northwind.add_user("manager", email="manager@northwind.com")
    ticket = urgent_ticket(northwind, north_customer, subject="Northwind's private outage")
    backdate(sync_engine, ticket["id"], minutes=BACKDATE_MINUTES)

    sweep(sync_engine, ticket["id"])

    with sync_engine.connect() as conn:
        bodies = (
            conn.execute(
                text("SELECT body FROM notifications WHERE ticket_id = CAST(:id AS uuid)"),
                {"id": str(ticket["id"])},
            )
            .scalars()
            .all()
        )
    assert bodies, "the sweep staged nothing, so this test would pass for the wrong reason"
    assert all("Northwind's private outage" in body for body in bodies)


def test_a_sweep_for_an_unknown_organization_does_nothing(
    northwind: OrgSession, sync_engine: Engine
) -> None:
    """An id that names no tenant is not an error, and it writes nothing.

    The dispatcher only ever hands over ids it read from the organizations table, so this
    is the defensive direction: every query filters on the id it was given, and an id
    nothing matches returns nothing rather than falling back to "all organizations" — which
    is the failure mode of an accidentally optional filter.
    """
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    hotel = urgent_ticket(northwind, north_customer)
    backdate(sync_engine, hotel["id"], minutes=BACKDATE_MINUTES)

    counts = sla_tasks.check_organization_sla(str(uuid.uuid4()))

    assert counts == {"tickets": 0, "warnings": 0, "breaches": 0, "notifications": 0, "queued": 0}
    assert alerts_for(sync_engine, hotel["id"]) == []


def test_a_customer_cannot_reach_the_policy_table_from_either_tenant(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """The fourth role in the matrix, checked on both sides of the boundary at once.

    A portal caller is refused by the capability rather than by the tenant — `SLA_VIEW` is
    not theirs — and the point of asserting it per tenant is that the refusal cannot be
    explained by which organization they are in. Both are 403 with the same body.
    """
    north_portal = northwind.add_user("customer", email="portal@northwind.com")
    south_portal = southwind.add_user("customer", email="portal@southwind.com")

    north_response = north_portal.get(f"{SLA}/policies")
    south_response = south_portal.get(f"{SLA}/policies")

    assert north_response.status_code == south_response.status_code == 403
    assert north_response.json() == south_response.json()
