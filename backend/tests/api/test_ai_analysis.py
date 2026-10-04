"""The two AI routes — what they answer, who may reach them, and what they refuse to redo.

**The route is where §16's *"the API should not wait unnecessarily for the LLM"* is made
observable.** Nothing in `app/api/ai.py` imports a provider, and the proof is in this file:
the tests below replace `ai_service._provider` with a fake that raises if it is ever called,
and every request in this suite still answers 201 and 202 with real rows. A provider call
that crept onto the request path would fail here rather than quietly making `POST /tickets`
slow.

**The two routes exist as a pair, and neither is `TICKET_VIEW`.** §3 gives customers no AI
access, so a customer refused the read route is not a redaction decision — it is the whole
capability, and `test_a_customer_cannot_ask_about_their_own_ticket` is the test that would
catch the obvious wrong guard (`TICKET_VIEW`, which every portal account holds).

**The ticket's own analysis is already in flight when a ticket is created.** Phase U queues
one from `ticket_service.create_ticket`, so the common case of `POST /ai/analyze` is a user
pressing a button on a ticket that is already being analyzed. That is not a degenerate case to
work around — it is §16's duplicate guard doing its job, and it is why the "asking twice" test
below asserts on the *enqueue count* rather than on the response, which is identical either
way by design.
"""

import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.ai.fake import FakeProvider
from app.core.config import get_settings
from app.services import ai_service
from tests.conftest import TICKETS, OrgSession

pytestmark = pytest.mark.integration

API_AI = "ai"


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Analysis API Co")


@pytest.fixture
def customer(org: OrgSession) -> dict[str, Any]:
    return org.add_customer(name="Grace Hopper", email="grace@navy.mil")


@pytest.fixture(autouse=True)
def no_provider_is_reachable(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    """A scripted provider with **no** outcomes, so any call at all is a hard failure.

    `FakeProvider` raises `AssertionError` when asked for an outcome it was not scripted for,
    which turns "this request path does not call a model" from an inspection of the imports
    into a property every test in this file enforces. It is autouse so a test cannot forget it
    and pass for the wrong reason.
    """
    provider = FakeProvider()
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


def ticket_path(ticket_id: object, *parts: str) -> str:
    return "/".join([f"{TICKETS}/{ticket_id}", *parts])


def rows_of(response: Any) -> list[dict[str, Any]]:
    assert response.status_code in (200, 202), response.text
    return cast("list[dict[str, Any]]", response.json())


# ---------------------------------------------------------------------------
# Raising a ticket queues its analysis — §18 steps 1 and 2
# ---------------------------------------------------------------------------


def test_raising_a_ticket_queues_one_analysis_and_answers_at_once(
    org: OrgSession, customer: dict[str, Any], queued_analyses: list[tuple[str, str, list[str]]]
) -> None:
    """201, and the analysis is a message to a broker rather than a call in this request.

    The two claims are the two halves of §16. The response is the created ticket, returned
    before any model was asked — the provider here is the empty fake, so a call would be an
    `AssertionError` rather than a slow test. And the queue is one message naming two rows,
    which is what makes the analysis visible on the ticket the moment it exists.
    """
    created = org.add_ticket(customer["id"], subject="Charged twice")

    assert created["status"] == "open"
    assert len(queued_analyses) == 1
    ticket_id, organization_id, analysis_ids = queued_analyses[0]
    assert ticket_id == created["id"]
    assert uuid.UUID(organization_id)
    assert len(analysis_ids) == 2

    queued = rows_of(org.get(ticket_path(created["id"], API_AI, "analyses")))
    assert [row["id"] for row in queued] == analysis_ids
    assert [row["operation"] for row in queued] == ["classify", "sentiment"]
    assert {row["status"] for row in queued} == {"pending"}
    # Nothing has been concluded yet, which is the state the read route exists to show.
    assert {row["result"] for row in queued} == {None}


# ---------------------------------------------------------------------------
# POST /tickets/{id}/ai/analyze
# ---------------------------------------------------------------------------


def test_asking_for_an_analysis_answers_202_with_the_pending_rows(
    org: OrgSession, customer: dict[str, Any], queued_analyses: list[tuple[str, str, list[str]]]
) -> None:
    """202 rather than 201: the body records what was queued, not what was concluded.

    The rows carry `provider` and `model`, which is §41's "make it clear this is AI-generated"
    as data — a client renders that rather than a hardcoded "AI" label that keeps making the
    claim after the row behind it changed.

    The ticket is already being analyzed, because creating it queued one. That is the point of
    the second assertion: pressing "analyze" on a fresh ticket answers 202 with the rows that
    exist and hands the broker **nothing**, so §16's duplicate guard is what stops two clicks
    from being four provider calls.
    """
    created = org.add_ticket(customer["id"])

    response = org.post(ticket_path(created["id"], API_AI, "analyze"))

    assert response.status_code == 202, response.text
    rows = rows_of(response)
    assert [row["operation"] for row in rows] == ["classify", "sentiment"]
    assert {row["status"] for row in rows} == {"pending"}
    settings = get_settings()
    for row in rows:
        assert row["provider"] == settings.AI_PROVIDER
        assert row["model"] == settings.AI_MODEL
        assert row["ticket_id"] == created["id"]

    # One enqueue for the whole test: the creation's. The button did not add a second.
    assert len(queued_analyses) == 1


def test_asking_twice_returns_the_same_rows_and_queues_nothing_new(
    org: OrgSession, customer: dict[str, Any], queued_analyses: list[tuple[str, str, list[str]]]
) -> None:
    """The idempotent case, asserted on identity of the rows rather than on a status code.

    A retry after a timeout and a double-click are the same request, and both must land on the
    rows the first one wrote: a second set would give the worker two pairs of ids to fill and
    the ticket two timelines to explain. The status code deliberately does not distinguish the
    two cases — "is this ticket being analyzed" has the same answer either way — so the
    assertion has to be about the ids.
    """
    created = org.add_ticket(customer["id"])

    first = rows_of(org.post(ticket_path(created["id"], API_AI, "analyze")))
    second = rows_of(org.post(ticket_path(created["id"], API_AI, "analyze")))

    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert len(queued_analyses) == 1


# ---------------------------------------------------------------------------
# Who may reach them
# ---------------------------------------------------------------------------


def test_an_assigned_agent_may_ask_and_read(org: OrgSession, customer: dict[str, Any]) -> None:
    """`AI_REQUEST_ANALYSIS` is held by agents and managers — §3's matrix, one row."""
    agent = org.add_user("agent", email="assigned@analysisapi.com")
    created = org.add_ticket(customer["id"])
    assigned = org.post(
        f"{TICKETS}/{created['id']}/assign", json={"assigned_agent_id": agent.user_id}
    )
    assert assigned.status_code == 200, assigned.text

    asked = agent.post(ticket_path(created["id"], API_AI, "analyze"))
    read = agent.get(ticket_path(created["id"], API_AI, "analyses"))

    assert asked.status_code == 202, asked.text
    assert read.status_code == 200, read.text
    assert len(read.json()) == 2


def test_a_customer_cannot_ask_about_their_own_ticket(org: OrgSession) -> None:
    """§3 gives the portal no AI access, and this is the test the wrong guard would fail.

    `TICKET_VIEW` is held by every portal account, so guarding these routes with it — the
    obvious choice, since a ticket is what they hang off — would hand a customer the analysis
    of their own ticket, including `error_message`, whose column comment reads *"surfaced to
    staff, never to customers: upstream errors can echo prompt content."*

    Asserted on both routes and on the §42 code rather than on the number alone, so a 403
    that came from somewhere else in the stack cannot pass for this one.
    """
    record = org.add_customer(name="Ada Lovelace", email="ada@analytical.engine")
    portal = org.add_portal_user(cast("str", record["id"]))
    ticket = org.add_ticket(cast("str", record["id"]))

    asked = portal.post(ticket_path(ticket["id"], API_AI, "analyze"))
    read = portal.get(ticket_path(ticket["id"], API_AI, "analyses"))

    for response in (asked, read):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "FORBIDDEN"


def test_an_unassigned_agent_cannot_see_another_agents_analysis(
    org: OrgSession, customer: dict[str, Any]
) -> None:
    """The row scope is applied to the analysis by way of its ticket, and a 404 is the answer.

    This is the property `ai_repository.latest_by_operation` gets by taking a `Ticket` instead
    of an id: the route resolves the ticket first, so there is no path to an analysis that did
    not pass the same check the ticket read passes. A colleague's ticket is a 404 and not a
    403 for the reason ADR-009 gives — an agent must not be able to size the desk's workload by
    watching which ids are refused.
    """
    colleague = org.add_user("agent", email="colleague@analysisapi.com")
    outsider = org.add_user("agent", email="outsider@analysisapi.com")
    created = org.add_ticket(customer["id"])
    org.post(f"{TICKETS}/{created['id']}/assign", json={"assigned_agent_id": colleague.user_id})

    refused = outsider.get(ticket_path(created["id"], API_AI, "analyses"))
    allowed = colleague.get(ticket_path(created["id"], API_AI, "analyses"))

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "TICKET_NOT_FOUND"
    assert allowed.status_code == 200, allowed.text


def test_a_ticket_in_another_tenant_is_a_404_on_both_routes(
    org: OrgSession,
    customer: dict[str, Any],
    register_org: Callable[..., OrgSession],
    queued_analyses: list[tuple[str, str, list[str]]],
) -> None:
    """A foreign ticket and a nonexistent one are the same answer, on both verbs.

    The tenant predicate is in the query rather than in a comparison afterwards, so there is
    nothing here that could return a body with the wrong tenant's analysis in it — the row is
    never loaded. The second assertion is the one that makes it a statement about isolation
    rather than about a 404: the first tenant's rows are still there and still readable by
    their owner.
    """
    created = org.add_ticket(customer["id"])
    stranger = register_org(organization_name="Other Co")

    asked = stranger.post(ticket_path(created["id"], API_AI, "analyze"))
    read = stranger.get(ticket_path(created["id"], API_AI, "analyses"))

    for response in (asked, read):
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"

    # Nothing was queued for the stranger's attempt, and the owner still sees their rows.
    assert len(queued_analyses) == 1
    assert len(rows_of(org.get(ticket_path(created["id"], API_AI, "analyses")))) == 2


def test_a_ticket_that_does_not_exist_is_a_404(
    org: OrgSession, no_provider_is_reachable: FakeProvider
) -> None:
    """A uuid that names nothing. The same answer as a foreign one, deliberately.

    Written out because a malformed or unknown id is the request a client bug produces most
    often, and it must not be the request that gets a different error shape — a 500 from a
    `None` reaching a serializer, say, which would be an authenticated caller's way to probe
    the deployment.
    """
    missing = uuid.uuid4()

    asked = org.post(ticket_path(missing, API_AI, "analyze"))
    read = org.get(ticket_path(missing, API_AI, "analyses"))

    for response in (asked, read):
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"
    # The empty fake is still never asked, which is the other half: a missing ticket must not
    # reach a provider either.
    assert no_provider_is_reachable.calls == 0


# ---------------------------------------------------------------------------
# §45 — the limit on the one route whose abuse is billed
# ---------------------------------------------------------------------------


def test_asking_is_rate_limited(
    org: OrgSession, customer: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route is wired to the limiter and renders 429 in the §42 envelope.

    Zero rather than a small number, for the reason `test_upload_is_rate_limited` gives about
    its own setting: the assertion must not depend on how much an earlier test has already
    counted against this key. The counting itself is covered against a stand-in client in
    `tests/unit/test_rate_limit.py`; this is the wiring, including the `Retry-After` a client
    needs in order to back off correctly.

    The route is *after* authentication in the router, which is why the key can be a user id at
    all — and why this is a limit on one account's spending rather than on a shared address.
    """
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_AI_PER_HOUR", 0, raising=False)
    created = org.add_ticket(customer["id"])

    response = org.post(ticket_path(created["id"], API_AI, "analyze"))

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.headers.get("Retry-After")


def test_the_read_route_is_not_rate_limited(
    org: OrgSession, customer: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading is not billed, so the limiter is on the verb that spends money and not the pair.

    A guard on the read route would be a limit on how often a client may *look* at work it has
    already paid for, which is the shape of limit that makes a UI feel broken without
    protecting anything. §45 names "AI endpoints", and the distinction that matters is which
    one costs tokens.
    """
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_AI_PER_HOUR", 0, raising=False)
    created = org.add_ticket(customer["id"])

    read = org.get(ticket_path(created["id"], API_AI, "analyses"))

    assert read.status_code == 200, read.text
    assert len(read.json()) == 2


# ---------------------------------------------------------------------------
# The refusal is authenticated
# ---------------------------------------------------------------------------


def test_an_unauthenticated_request_never_reaches_the_limiter(client: TestClient) -> None:
    """401 before any counting, which is what keeps the limit's subject the caller.

    The limiter is declared beside the capability dependency, so an anonymous request is
    refused by the auth layer first. That ordering is what makes the key a user id rather than
    an address — the same reading ADR-014 takes for uploads and ADR-022 declines to repeat for
    logins.
    """
    response = client.post(f"{TICKETS}/{uuid.uuid4()}/{API_AI}/analyze")

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
