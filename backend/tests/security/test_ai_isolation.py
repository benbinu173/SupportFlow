"""AI across two tenants: an analysis is reachable exactly when its ticket is, and nowhere else.

Phase U adds the first table in this application that a **worker writes** and a request reads,
and the worker is the part that makes the isolation question different from every other one in
this package. `app/repositories/ai_repository.py`'s two worker queries take `organization_id`
as an explicit argument — there is no `TenantScopedRepository` behind them to inherit a filter
from, the same way `sla_repository.py`'s are, and for the same reason: a task has no request to
read a tenant off. So the predicate is *written out*, and a missing `organization_id` clause in
one of those functions would be a cross-tenant write that no API test could catch, because no
API path calls them.

**Two tests below therefore drive the task directly, with a mismatched tenant in each
direction.** One hands the worker Northwind's ticket under Southwind's identity; the other
hands it Northwind's row ids under Southwind's identity while naming Northwind's ticket. Both
must do nothing at all — and "nothing" is asserted as *no provider call*, because a run that
quietly analyzed the wrong row and a run that did nothing would both return zeroes from a
count of *completed* under a provider that was never scripted to answer.

**The cross-tenant refusal is a 404 on the ticket, not a 403.** There is no
`GET /ai/analyses/{id}`: an analysis is addressed through the ticket that owns it, so there is
no id to guess and no id to refuse — which is exactly why `app/api/ai.py` mounts under
`/tickets/{ticket_id}` rather than at a root (ADR-015). The comparison against a ticket that
never existed is in full, body and headers, because a status code alone is not the property.
"""

import uuid
from collections.abc import Callable
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai.errors import AIPermanentError
from app.ai.fake import FakeProvider
from app.services import ai_service
from app.workers import ai_tasks
from tests.conftest import TICKETS, OrgSession

pytestmark = pytest.mark.security


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the other isolation files do."""
    # Nothing to do in the body: `truncate_tables` yields, so depending on it is what places
    # the truncation on the far side of the test.


@pytest.fixture
def northwind(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Northwind AI")


@pytest.fixture
def southwind(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Southwind AI")


@pytest.fixture(autouse=True)
def no_provider_is_reachable(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    """A scripted provider with no outcomes, so any call is a hard failure. **Autouse.**

    The two worker tests below are about a task that must do *nothing*. A count of completed
    rows would be zero whether the task skipped the work or performed it against a provider
    that answered — so the assertion has to be about whether the provider was asked at all, and
    the loudest way to state that is a fake which raises when it is.
    """
    provider = FakeProvider()
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def analyses_path(ticket_id: object) -> str:
    return f"{TICKETS}/{ticket_id}/ai/analyses"


def analyze_path(ticket_id: object) -> str:
    return f"{TICKETS}/{ticket_id}/ai/analyze"


def row_ids(session: OrgSession, ticket_id: object) -> list[str]:
    response = session.get(analyses_path(ticket_id))
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


def organization_of(engine: Engine, ticket_id: object) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT organization_id FROM tickets WHERE id = CAST(:id AS uuid)"),
                {"id": str(ticket_id)},
            ).scalar_one()
        )


def ledger_tenants(engine: Engine, ticket_id: object) -> set[str]:
    """The organizations named on a ticket's ledger rows.

    Read past the API because the assertion is about *which tenant a row was written for*, and
    a row attributed to the wrong tenant would be invisible through an endpoint that only ever
    shows a caller their own — the row is the evidence.
    """
    with engine.connect() as conn:
        return {
            str(value)
            for value in conn.execute(
                text(
                    "SELECT DISTINCT organization_id FROM ai_usage "
                    "WHERE ticket_id = CAST(:id AS uuid)"
                ),
                {"id": str(ticket_id)},
            ).scalars()
        }


def statuses(engine: Engine, ticket_id: object) -> list[str]:
    """Every analysis row's status for a ticket, read from the table.

    From the table and not the route because the route serves *latest per operation*, which
    would hide a second row having appeared — and "no new rows were written" is one of the
    things a leaked tenant filter would produce.
    """
    with engine.connect() as conn:
        return sorted(
            str(value)
            for value in conn.execute(
                text(
                    "SELECT CAST(status AS text) FROM ai_analyses "
                    "WHERE ticket_id = CAST(:id AS uuid)"
                ),
                {"id": str(ticket_id)},
            ).scalars()
        )


# ---------------------------------------------------------------------------
# The route, across the boundary
# ---------------------------------------------------------------------------


def test_each_tenant_reads_only_its_own_analyses(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """Two tenants, one analysis each, and neither route serves the other's rows.

    The positive half is asserted as well as the refusal, because a route that refused
    everything would pass a test made only of 404s. Northwind's own ticket reads back its two
    rows to Northwind and nothing to Southwind, and the mirror holds — which is what makes this
    a statement about the filter rather than about the absence of a feature.
    """
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    south_customer = str(southwind.add_customer(name="Ada", email="ada@southwind.com")["id"])
    north_ticket = northwind.add_ticket(north_customer, subject="Northwind's outage")
    south_ticket = southwind.add_ticket(south_customer, subject="Southwind's outage")

    north_rows = northwind.get(analyses_path(north_ticket["id"]))
    south_rows = southwind.get(analyses_path(south_ticket["id"]))
    crossed = southwind.get(analyses_path(north_ticket["id"]))

    assert north_rows.status_code == south_rows.status_code == 200
    assert {row["operation"] for row in north_rows.json()} == {"classify", "sentiment"}
    assert {row["operation"] for row in south_rows.json()} == {"classify", "sentiment"}
    assert crossed.status_code == 404
    # The other tenant's ticket id is not merely refused — its subject never appears, which is
    # what makes this about the row rather than about the status code.
    assert "Northwind's outage" not in crossed.text


def test_the_refusal_is_identical_to_a_record_that_never_existed(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """Status, error code, message and body — compared in full, on both verbs.

    A cross-tenant answer that said "that analysis belongs to another organization" would leak
    exactly as much as a 403 while passing a status-only assertion. `POST` is included because a
    route that leaked on the write side would be the more damaging one: it would queue work
    against another tenant's ticket and charge it to the caller's ledger.
    """
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    north_ticket = northwind.add_ticket(north_customer)
    missing = uuid.uuid4()

    for verb, path in (("get", analyses_path), ("post", analyze_path)):
        crossed = getattr(southwind, verb)(path(north_ticket["id"]))
        never_existed = getattr(southwind, verb)(path(missing))

        assert crossed.status_code == never_existed.status_code == 404
        assert crossed.json() == never_existed.json()
        assert crossed.headers.get("WWW-Authenticate") is None


def test_a_customer_cannot_read_either_tenants_analyses(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """The fourth role in the matrix, checked on both sides at once.

    A portal caller is refused by the capability rather than by the tenant — `TICKET_VIEW` does
    not reach these routes — and asserting it per tenant is what stops the refusal being
    explained by which organization they are in. Both answers are 403 with the same body, and
    each is about their *own* ticket, which is the one a wrong guard would have served.

    The ticket is raised for the customer the portal account is linked to, so the `own` row
    scope can resolve and a 404 is not doing the work the 403 is supposed to be demonstrating.
    """
    north_record = northwind.add_customer(name="Grace", email="grace@northwind.com")
    south_record = southwind.add_customer(name="Ada", email="ada@southwind.com")
    north_portal = northwind.add_portal_user(cast("str", north_record["id"]))
    south_portal = southwind.add_portal_user(cast("str", south_record["id"]))
    north_ticket = northwind.add_ticket(cast("str", north_record["id"]))
    south_ticket = southwind.add_ticket(cast("str", south_record["id"]))

    north_response = north_portal.get(analyses_path(north_ticket["id"]))
    south_response = south_portal.get(analyses_path(south_ticket["id"]))

    assert north_response.status_code == south_response.status_code == 403
    assert north_response.json() == south_response.json()


# ---------------------------------------------------------------------------
# The worker, which has no request to read a tenant from
# ---------------------------------------------------------------------------


def test_the_worker_will_not_analyze_another_tenants_ticket(
    northwind: OrgSession,
    southwind: OrgSession,
    sync_engine: Engine,
    no_provider_is_reachable: FakeProvider,
) -> None:
    """A task handed Northwind's ticket under Southwind's identity does nothing at all.

    This is `ai_repository.load_ticket`'s `organization_id` predicate, which is the only thing
    standing between a worker and guessing which tenant it acts for: the task is handed an
    organization id over a broker, and a redelivered or misconfigured message could carry the
    wrong one. A missing clause here would analyze another tenant's ticket, write its columns,
    and stage **ledger rows naming the caller's tenant** — spend attributed to an organization
    that never asked for it.

    Nothing happens, which is asserted three ways: no provider call, both of Northwind's rows
    still `pending`, and no ledger row exists at all. The last one is the part a count of
    completions would have missed.
    """
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    south_customer = str(southwind.add_customer(name="Ada", email="ada@southwind.com")["id"])
    north_ticket = northwind.add_ticket(north_customer)
    south_ticket = southwind.add_ticket(south_customer)

    counts = ai_tasks.analyze_ticket(
        north_ticket["id"],
        organization_of(sync_engine, south_ticket["id"]),
        row_ids(northwind, north_ticket["id"]),
    )

    assert counts == {"completed": 0, "failed": 0, "skipped": 0}
    assert no_provider_is_reachable.calls == 0
    assert statuses(sync_engine, north_ticket["id"]) == ["pending", "pending"]
    assert ledger_tenants(sync_engine, north_ticket["id"]) == set()


def test_the_worker_will_not_load_another_tenants_rows(
    northwind: OrgSession,
    southwind: OrgSession,
    sync_engine: Engine,
    no_provider_is_reachable: FakeProvider,
) -> None:
    """The second filter, isolated from the first: right ticket, right tenant, wrong rows.

    `load_ticket` and `load_analyses` each carry their own `organization_id` predicate, and the
    test above only exercises the first because a foreign ticket never reaches the second. Here
    the ticket and the organization are both correct — Northwind's own — while the row ids are
    Southwind's, which is the shape a misrouted message takes when the tenant on the task is
    right and the ids are not.

    The ticket loads. The rows do not. The task therefore has nothing to claim and returns a
    run of zeroes, and Northwind's own rows are still `pending` — untouched, because
    `load_analyses` filtered on a tenant they do not carry the ids for.
    """
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    south_customer = str(southwind.add_customer(name="Ada", email="ada@southwind.com")["id"])
    north_ticket = northwind.add_ticket(north_customer)
    south_ticket = southwind.add_ticket(south_customer)

    counts = ai_tasks.analyze_ticket(
        north_ticket["id"],
        organization_of(sync_engine, north_ticket["id"]),
        row_ids(southwind, south_ticket["id"]),
    )

    assert counts == {"completed": 0, "failed": 0, "skipped": 0}
    assert no_provider_is_reachable.calls == 0
    assert statuses(sync_engine, north_ticket["id"]) == ["pending", "pending"]
    assert statuses(sync_engine, south_ticket["id"]) == ["pending", "pending"]


def test_a_run_charges_its_ledger_to_its_own_tenant(
    northwind: OrgSession,
    southwind: OrgSession,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive half of the two tests above: a correct run names one tenant, its own.

    Without this, both of those would pass against a repository that filtered on something so
    strict that no run ever worked. The worker is run for Northwind's ticket with Northwind's
    identity, and every ledger row it writes names Northwind — with `/analytics/overview`
    grouping by exactly this column, a row attributed elsewhere would be spend no tenant could
    see and one tenant would be shown a cost it never incurred.

    Both operations are refused rather than answered, which keeps this test about the ledger:
    a failing call still writes its row, and using failures avoids threading a scripted success
    through a fixture whose whole purpose is to make an unscripted call impossible.
    """
    north_customer = str(northwind.add_customer(name="Grace", email="grace@northwind.com")["id"])
    southwind.add_ticket(str(southwind.add_customer(name="Ada", email="ada@southwind.com")["id"]))
    north_ticket = northwind.add_ticket(north_customer)
    failing = FakeProvider(
        AIPermanentError("refused"),
        AIPermanentError("refused"),
        prompt_tokens=0,
        completion_tokens=0,
    )
    monkeypatch.setattr(ai_service, "_provider", lambda: failing)

    counts = ai_tasks.analyze_ticket(
        north_ticket["id"],
        organization_of(sync_engine, north_ticket["id"]),
        row_ids(northwind, north_ticket["id"]),
    )

    assert counts == {"completed": 0, "failed": 2, "skipped": 0}
    assert failing.calls == 2
    assert ledger_tenants(sync_engine, north_ticket["id"]) == {
        organization_of(sync_engine, north_ticket["id"])
    }
