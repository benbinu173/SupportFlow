"""Row scope: how much of a tenant each actor may reach.

Tenant isolation and row scope are different questions, and `test_tenant_isolation.py`
answers only the first — one organization cannot reach another's rows. This file answers
the second: *within* an organization, how many rows does a given role reach?

`docs/requirements.md` §3 qualifies several capabilities with `own` or `assigned`. The
capability is one — "view ticket detail" — and three roles hold it; what differs is how
many rows it reaches. That cannot be decided at the route, because at the route there is
no row yet, so the decision lives in `app/repositories/scoping.py` and this file holds it
to account.

The failure mode this file exists for is the quiet one: a scope that fails **open**. The
specific shape it guards against is a predicate composed so that the narrowing query
parameter and the scope predicate are alternatives rather than both required — an agent
asking for `assigned_agent_id=<someone else>` and receiving that agent's queue.

The file is in two halves on purpose:

* **The mechanism**, exercised against the repository with hand-built rows. This is where
  an unlinked portal account can be constructed at all — the API refuses to create one,
  which is the right behaviour and also the reason it can never be reached over HTTP. A
  scope that fails closed is a claim about a state that only exists below the API, so it
  can only be tested below the API.
* **The surface**, over real requests, checking the scopes the API does produce against
  the rows a caller can actually see.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.models.customer import Customer
from app.models.enums import UserRole
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.user import User
from app.repositories.ticket_repository import TicketRepository
from tests.conftest import TICKETS, OrgSession

pytestmark = pytest.mark.security

# A placeholder, not a credential: nothing in this file authenticates. Argon2 hashes are
# 90-odd characters and a real one here would suggest something verifies it.
NOT_A_CREDENTIAL = "unused-in-this-test"


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test — the HTTP half writes through the real API.

    Declared per-file rather than in `tests/security/conftest.py`, following the same
    reasoning as the sibling file: the route-protection test in this package inspects the
    routing table and issues no requests, so a package-wide autouse fixture would make it
    require a running Postgres for nothing.
    """


# ---------------------------------------------------------------------------
# The mechanism, at the repository
# ---------------------------------------------------------------------------


@pytest.fixture
async def scope_rows(db: AsyncSession) -> dict[str, Any]:
    """Two organizations, four tickets, and every interesting vantage point on them.

    Built through the ORM rather than the API because the same rows have to be visible
    from eight different contexts, and because one of those contexts cannot be produced by
    the API at all.

    The shape is chosen so that no two contexts have the same answer: if the ticket sets
    were distinguishable only by organization, a broken row scope would still pass every
    assertion here.
    """
    scopeworks = Organization(name="Scopeworks", slug=f"scopeworks-{uuid.uuid4().hex[:8]}")
    elsewhere = Organization(name="Elsewhere", slug=f"elsewhere-{uuid.uuid4().hex[:8]}")
    db.add_all([scopeworks, elsewhere])
    await db.flush()

    ada = Customer(organization_id=scopeworks.id, name="Ada", email="ada@scopeworks.com")
    grace = Customer(organization_id=scopeworks.id, name="Grace", email="grace@scopeworks.com")
    stranger = Customer(organization_id=elsewhere.id, name="Stranger", email="s@elsewhere.com")
    db.add_all([ada, grace, stranger])
    await db.flush()

    priya = User(
        organization_id=scopeworks.id,
        name="Priya",
        email="priya@scopeworks.com",
        password_hash=NOT_A_CREDENTIAL,
        role=UserRole.AGENT,
    )
    quinn = User(
        organization_id=scopeworks.id,
        name="Quinn",
        email="quinn@scopeworks.com",
        password_hash=NOT_A_CREDENTIAL,
        role=UserRole.AGENT,
    )
    db.add_all([priya, quinn])
    await db.flush()

    def ticket(number: int, subject: str, customer: Customer, agent: User | None) -> Ticket:
        return Ticket(
            organization_id=scopeworks.id,
            number=number,
            customer_id=customer.id,
            assigned_agent_id=agent.id if agent else None,
            subject=subject,
            description="Body.",
        )

    tickets = {
        "ada_by_priya": ticket(1, "Ada's ticket, with Priya", ada, priya),
        "grace_by_priya": ticket(2, "Grace's ticket, with Priya", grace, priya),
        "ada_unassigned": ticket(3, "Ada's ticket, unassigned", ada, None),
        "grace_by_quinn": ticket(4, "Grace's ticket, with Quinn", grace, quinn),
        "another_tenant": Ticket(
            organization_id=elsewhere.id,
            number=1,
            customer_id=stranger.id,
            subject="Belongs to somewhere else",
            description="Body.",
        ),
    }
    db.add_all(list(tickets.values()))
    await db.flush()

    return {
        "organization_id": scopeworks.id,
        "other_organization_id": elsewhere.id,
        "customers": {"ada": ada.id, "grace": grace.id},
        "agents": {"priya": priya.id, "quinn": quinn.id},
        "tickets": {name: row.id for name, row in tickets.items()},
    }


def _context(
    rows: dict[str, Any],
    role: UserRole,
    *,
    user: uuid.UUID | None = None,
    customer: uuid.UUID | None = None,
) -> TenantContext:
    """A context for one vantage point on the fixture rows.

    Built by hand, which is the point of this half of the file: the API has exactly one
    place that constructs a `TenantContext`, and it will not construct this one.
    """
    return TenantContext(
        user_id=user if user is not None else rows["agents"]["priya"],
        organization_id=rows["organization_id"],
        role=role,
        customer_id=customer,
    )


async def _subjects(session: AsyncSession, context: TenantContext, **filters: Any) -> set[str]:
    """The subjects of the tickets this context can reach, via the real repository."""
    repository = TicketRepository(session, context)
    page = await repository.list_tickets(limit=50, **filters)
    return {ticket.subject for ticket in page}


async def test_an_admin_reaches_every_ticket_in_the_organization(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    """The baseline. Without it, every other assertion here could pass against a
    repository that simply returns nothing."""
    subjects = await _subjects(db, _context(scope_rows, UserRole.ADMIN))

    assert subjects == {
        "Ada's ticket, with Priya",
        "Grace's ticket, with Priya",
        "Ada's ticket, unassigned",
        "Grace's ticket, with Quinn",
    }


async def test_a_manager_reaches_every_ticket_in_the_organization(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    assert len(await _subjects(db, _context(scope_rows, UserRole.MANAGER))) == 4


async def test_an_agent_reaches_only_their_own_assignments(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    """`assigned`, not "assigned or unassigned": an unassigned ticket is not the
    agent's to see until someone gives it to them, which is what makes assignment the
    act that puts a ticket on a queue."""
    agents = scope_rows["agents"]

    priya = await _subjects(db, _context(scope_rows, UserRole.AGENT, user=agents["priya"]))
    quinn = await _subjects(db, _context(scope_rows, UserRole.AGENT, user=agents["quinn"]))

    assert priya == {"Ada's ticket, with Priya", "Grace's ticket, with Priya"}
    assert quinn == {"Grace's ticket, with Quinn"}


async def test_a_customer_reaches_only_their_own_tickets(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    customers = scope_rows["customers"]

    ada = await _subjects(db, _context(scope_rows, UserRole.CUSTOMER, customer=customers["ada"]))
    grace = await _subjects(
        db, _context(scope_rows, UserRole.CUSTOMER, customer=customers["grace"])
    )

    assert ada == {"Ada's ticket, with Priya", "Ada's ticket, unassigned"}
    assert grace == {"Grace's ticket, with Priya", "Grace's ticket, with Quinn"}


async def test_an_unlinked_customer_reaches_nothing(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    """The fail-closed case, and the reason this half of the file exists.

    A portal account with no linked `Customer` row has no answer to "which tickets are
    mine", and there are two ways to get that wrong. The obvious one is to treat a
    missing customer as unrestricted, which hands a portal account the whole
    organization. The subtler one is to leave it to `customer_id = NULL`, which matches
    nothing *by accident* — SQL's three-valued logic makes the comparison unknown rather
    than true. That happens to be correct here and stops being correct the moment the
    predicate is composed differently, which is exactly the kind of correctness that does
    not survive a refactor.

    So the unlinked case is written out as `false()` and asserted to match nothing, while
    the customer it *could* have claimed sits right there in the organization.
    """
    unlinked = _context(scope_rows, UserRole.CUSTOMER, customer=None)

    assert await _subjects(db, unlinked) == set()

    # Not vacuous: the organization does hold tickets, and this user is in it.
    assert len(await _subjects(db, _context(scope_rows, UserRole.ADMIN))) == 4
    assert unlinked.organization_id == scope_rows["organization_id"]


async def test_an_unlinked_customer_cannot_read_one_ticket_either(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    """Failing closed has to hold on the single-row read as well as the list.

    The two go through different repository methods, so an unlinked account could
    plausibly get an empty page and a readable detail — a subtle asymmetry that a
    test of the list alone would not notice.
    """
    unlinked = _context(scope_rows, UserRole.CUSTOMER, customer=None)
    repository = TicketRepository(db, unlinked)

    for ticket_id in scope_rows["tickets"].values():
        assert await repository.get_visible(ticket_id) is None


async def test_no_scope_reaches_another_organization(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    """Every context, against the foreign ticket.

    The tenant predicate is `TenantScopedRepository`'s, not this module's, and this
    asserts the two compose rather than one replacing the other.
    """
    foreign = scope_rows["tickets"]["another_tenant"]
    customers = scope_rows["customers"]

    contexts = [
        _context(scope_rows, UserRole.ADMIN),
        _context(scope_rows, UserRole.MANAGER),
        _context(scope_rows, UserRole.AGENT, user=scope_rows["agents"]["priya"]),
        _context(scope_rows, UserRole.CUSTOMER, customer=customers["ada"]),
        _context(scope_rows, UserRole.CUSTOMER, customer=None),
    ]

    for context in contexts:
        assert await TicketRepository(db, context).get_visible(foreign) is None


async def test_a_filter_cannot_widen_an_agent_s_scope(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    """The query-parameter attack, at its source.

    `assigned_agent_id` is a narrowing filter, so an agent asking for Quinn's tickets
    must get the empty set — the intersection of "assigned to me" and "assigned to
    Quinn" is nothing. The bug this catches is a repository that builds its criteria as
    `scope OR filter`, which is the natural way to write "let the caller override the
    default" and is the whole vulnerability.
    """
    priya = _context(scope_rows, UserRole.AGENT, user=scope_rows["agents"]["priya"])

    assert await _subjects(db, priya, assigned_agent_id=scope_rows["agents"]["quinn"]) == set()
    # And asking for their own still works, so the filter is not simply being ignored.
    assert len(await _subjects(db, priya, assigned_agent_id=scope_rows["agents"]["priya"])) == 2


async def test_a_filter_cannot_widen_a_customer_s_scope(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    """The same attack with `customer_id`: Ada asking for Grace's tickets."""
    ada = _context(scope_rows, UserRole.CUSTOMER, customer=scope_rows["customers"]["ada"])

    assert await _subjects(db, ada, customer_id=scope_rows["customers"]["grace"]) == set()
    # And her own still work, so the filter is not simply being ignored.
    assert await _subjects(db, ada, customer_id=scope_rows["customers"]["ada"]) == {
        "Ada's ticket, with Priya",
        "Ada's ticket, unassigned",
    }


async def test_an_agent_cannot_see_an_unassigned_ticket_by_filtering_for_an_empty_assignee(
    db: AsyncSession, scope_rows: dict[str, Any]
) -> None:
    """`assigned_agent_id=None` means "no filter" at the route, so a caller cannot ask
    for "unassigned tickets" and thereby reach the whole queue.

    Worth its own test because the sentinel is the subtle part: a parameter that is
    *absent* and a parameter that is *null* are easy to conflate, and the reading that
    conflates them is the one that would let `?assigned_agent_id=` mean "everything".
    """
    priya = _context(scope_rows, UserRole.AGENT, user=scope_rows["agents"]["priya"])

    subjects = await _subjects(db, priya)

    assert "Ada's ticket, unassigned" not in subjects


# ---------------------------------------------------------------------------
# The surface, over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def scoped_org(register_org: Callable[..., OrgSession]) -> dict[str, Any]:
    """One organization with two customers, two agents, and a ticket each way.

    Raised through the real endpoints, so the contexts under test are the contexts the
    API actually builds — the repository half above proves the mechanism, and this half
    proves the mechanism is what the request path reaches.
    """
    admin = register_org(organization_name="Scopeworks")
    ada = admin.add_customer(name="Ada", email="ada@scopeworks.com")
    grace = admin.add_customer(name="Grace", email="grace@scopeworks.com")

    priya = admin.add_user("agent", name="Priya", email="priya@scopeworks.com")
    quinn = admin.add_user("agent", name="Quinn", email="quinn@scopeworks.com")

    ada_ticket = admin.add_ticket(ada["id"], subject="Ada's ticket")
    grace_ticket = admin.add_ticket(grace["id"], subject="Grace's ticket")

    # Priya takes one of each; Quinn takes nothing.
    for ticket in (ada_ticket, grace_ticket):
        assigned = admin.post(
            f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": priya.user_id}
        )
        assert assigned.status_code == 200, assigned.text

    return {
        "admin": admin,
        "agent": priya,
        "idle_agent": quinn,
        "ada_portal": admin.add_portal_user(ada["id"], email="ada@scopeworks.com"),
        "grace_portal": admin.add_portal_user(grace["id"], email="grace@scopeworks.com"),
        "tickets": {"ada": ada_ticket, "grace": grace_ticket},
    }


def _visible(session: OrgSession, **params: Any) -> set[str]:
    response = session.get(TICKETS, params=params)
    assert response.status_code == 200, response.text
    return {ticket["subject"] for ticket in response.json()}


def test_the_queue_a_role_sees_is_what_the_matrix_assigns(
    scoped_org: dict[str, Any],
) -> None:
    """All four roles in one assertion, so a change that helps one and breaks another is
    visible rather than discovered later in a different test."""
    both = {"Ada's ticket", "Grace's ticket"}

    assert _visible(scoped_org["admin"]) == both
    assert _visible(scoped_org["agent"]) == both
    assert _visible(scoped_org["ada_portal"]) == {"Ada's ticket"}
    assert _visible(scoped_org["grace_portal"]) == {"Grace's ticket"}


def test_an_agent_with_nothing_assigned_sees_an_empty_queue(
    scoped_org: dict[str, Any],
) -> None:
    """An empty page, not an error and not the organization's backlog.

    This is the state a new agent is in on their first morning, so it has to be a real
    answer rather than an edge case nobody looked at.
    """
    assert _visible(scoped_org["idle_agent"]) == set()


def test_a_query_parameter_cannot_widen_an_agent_s_queue_over_http(
    scoped_org: dict[str, Any],
) -> None:
    """The same attack as the repository test above, driven through the route — because
    the repository being right and the route passing the parameter through in a way that
    bypasses it are two different failures."""
    agent = scoped_org["agent"]
    idle = scoped_org["idle_agent"]

    assert _visible(agent, assigned_agent_id=idle.user_id) == set()
    assert _visible(agent, assigned_agent_id=agent.user_id) == {
        "Ada's ticket",
        "Grace's ticket",
    }


def test_a_query_parameter_cannot_widen_a_customer_s_queue_over_http(
    scoped_org: dict[str, Any],
) -> None:
    """Ada has the `TICKET_LIST` capability and can name her own `customer_id`. Naming
    someone else's must intersect to nothing rather than substitute."""
    ada = scoped_org["ada_portal"]

    assert _visible(ada, customer_id=scoped_org["admin"].user_id) == set()
    assert _visible(ada) == {"Ada's ticket"}


def test_a_customer_cannot_read_a_ticket_they_cannot_list(
    scoped_org: dict[str, Any],
) -> None:
    """The list and the detail agree. An agent or customer who gets an empty page and a
    readable row has found the scope being applied in one place and not the other."""
    ada = scoped_org["ada_portal"]
    grace_ticket = scoped_org["tickets"]["grace"]["id"]

    detail = ada.get(f"{TICKETS}/{grace_ticket}")

    assert detail.status_code == 404
    assert detail.json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_an_agent_cannot_read_a_colleague_s_ticket(
    scoped_org: dict[str, Any],
) -> None:
    """404 rather than 403, for the same reason the tenant tests use it: a 403 would
    confirm the ticket exists and is merely someone else's, which lets an agent map a
    colleague's workload by watching which ids are refused."""
    idle = scoped_org["idle_agent"]
    ada_ticket = scoped_org["tickets"]["ada"]["id"]

    detail = idle.get(f"{TICKETS}/{ada_ticket}")

    assert detail.status_code == 404
    assert detail.json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_an_agent_cannot_reach_a_colleague_s_thread_or_notes(
    scoped_org: dict[str, Any],
) -> None:
    """Every message route resolves the ticket first, so all three refuse together. A
    route that resolved messages and filtered them afterwards would still be a correct
    answer here — but only by coincidence, and it would not stay one."""
    idle = scoped_org["idle_agent"]
    ada_ticket = scoped_org["tickets"]["ada"]["id"]

    assert idle.get(f"{TICKETS}/{ada_ticket}/messages").status_code == 404
    assert idle.post(f"{TICKETS}/{ada_ticket}/messages", json={"body": "Hello?"}).status_code == 404
    assert idle.post(f"{TICKETS}/{ada_ticket}/notes", json={"body": "Note."}).status_code == 404


def test_a_customer_cannot_reach_another_customer_s_thread(
    scoped_org: dict[str, Any],
) -> None:
    ada = scoped_org["ada_portal"]
    grace_ticket = scoped_org["tickets"]["grace"]["id"]

    response = ada.get(f"{TICKETS}/{grace_ticket}/messages")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_an_agent_cannot_reach_a_colleague_s_ticket_by_asking_for_its_timeline(
    scoped_org: dict[str, Any],
) -> None:
    """The second route to the same row, which is where scope is most easily forgotten —
    it was added after the detail route and had its own guard written."""
    idle = scoped_org["idle_agent"]
    ada_ticket = scoped_org["tickets"]["ada"]["id"]

    assert idle.get(f"{TICKETS}/{ada_ticket}/events").status_code == 404


@pytest.mark.parametrize(
    ("action", "body"),
    [
        # `assigned → in_progress` is a legal edge, deliberately. An illegal one would
        # be refused on its own merits, so a 404 would say nothing about the scope and
        # the test would pass against a repository with no row scope at all.
        ("status", {"status": "in_progress"}),
        ("close", None),
        ("reopen", None),
    ],
)
def test_no_status_route_lets_an_agent_reach_a_colleague_s_ticket(
    scoped_org: dict[str, Any], action: str, body: dict[str, str] | None
) -> None:
    """The write side of the same claim.

    A scope that narrows reads but not writes is a filter, not a boundary, and a single
    forgotten guard would let an agent close a colleague's ticket — a change the owner
    would then have to notice from the timeline.

    Only the three status routes appear here. `/assign` and `/priority` are absent
    because an agent does not hold those capabilities at all, so they answer 403 before
    any row is loaded; that is a statement about the matrix, and
    `tests/api/test_tickets.py` already makes it. Testing them here would assert a
    refusal for the wrong reason and, worse, would keep passing if the row scope broke.
    """
    idle = scoped_org["idle_agent"]
    ada_ticket = scoped_org["tickets"]["ada"]["id"]

    response = idle.post(f"{TICKETS}/{ada_ticket}/{action}", json=body)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_a_refused_action_leaves_the_ticket_alone(
    scoped_org: dict[str, Any],
) -> None:
    """The 404 has to mean "nothing happened".

    Checked by asking the ticket's owner, so a refusal that still wrote an event — or
    moved the status without committing a response — would be caught rather than
    inferred from the refusal itself. The status asked for is a legal edge, so a broken
    scope would have produced a visible change rather than a second refusal.
    """
    idle = scoped_org["idle_agent"]
    admin = scoped_org["admin"]
    ada_ticket = scoped_org["tickets"]["ada"]

    refused = idle.post(f"{TICKETS}/{ada_ticket['id']}/status", json={"status": "in_progress"})
    assert refused.status_code == 404, refused.text

    after = admin.get(f"{TICKETS}/{ada_ticket['id']}").json()
    assert after["status"] == "assigned"
    timeline = admin.get(f"{TICKETS}/{ada_ticket['id']}/events").json()
    assert "status_changed" not in {event["event_type"] for event in timeline}

    # The owner can still make the same change, which is what makes the refusal above a
    # statement about who asked rather than about the request.
    allowed = scoped_org["agent"].post(
        f"{TICKETS}/{ada_ticket['id']}/status", json={"status": "in_progress"}
    )
    assert allowed.status_code == 200, allowed.text
