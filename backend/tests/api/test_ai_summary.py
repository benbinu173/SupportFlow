"""§36's second AI route — what it answers, who may reach it, and what it refuses to redo.

**The route is where §16's *"the API should not wait unnecessarily for the LLM"* stays
observable.** Nothing in `app/api/ai.py` imports a provider, and the proof is the autouse
fixture below: the tests here replace `ai_service._provider` with a fake that raises the
moment it is asked for an outcome, and every request still answers 202 with a real row. A
provider call that crept onto the request path would fail here rather than quietly making
summarizing slow — and for this route the temptation is real, because the freshness check
means a request *can* answer with a finished summary without any work happening at all.

**Both outcomes are `202`, and the status code is not asked to tell them apart.** A queued
summary comes back `pending` and a current one comes back `completed`; the same reading
`/analyze` takes of a double-click, and the reason is the same — from the caller's point of
view "what is the summary of this conversation" has an answer either way. The row says which
happened, and the enqueue count is what makes "no second call" a fact rather than an
inference.

**`AI_REQUEST_ANALYSIS` and never `TICKET_VIEW`.** §3 gives the portal no AI access at all,
so a customer refused here is not a redaction decision — it is the whole capability, and
`test_a_customer_cannot_summarize_their_own_ticket` is the test the obvious wrong guard
would fail.
"""

import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.ai.fake import FakeProvider
from app.core.config import get_settings
from app.services import ai_service
from app.workers import ai_tasks
from tests.conftest import TICKETS, OrgSession

pytestmark = pytest.mark.integration

API_AI = "ai"

SUMMARY = {"summary": "The customer cannot download their policy document in any browser."}


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Summary API Co")


@pytest.fixture
def customer(org: OrgSession) -> dict[str, Any]:
    return org.add_customer(name="Grace Hopper", email="grace@navy.mil")


@pytest.fixture
def portal(org: OrgSession, customer: dict[str, Any]) -> OrgSession:
    return org.add_portal_user(customer["id"])


@pytest.fixture(autouse=True)
def no_provider_is_reachable(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    """A scripted provider with **no** outcomes, so any call at all is a hard failure.

    Autouse for `tests/api/test_ai_analysis.py`'s reason: it turns "this request path does not
    call a model" from an inspection of the imports into a property every test in this file
    enforces, and it cannot be forgotten. The one test that has to complete a summary overrides
    it, deliberately and visibly.
    """
    provider = FakeProvider()
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


def summarize_path(ticket_id: object) -> str:
    return f"{TICKETS}/{ticket_id}/{API_AI}/summarize"


def reply(portal: OrgSession, ticket_id: object, body: str = "It will not download.") -> None:
    """Post the customer's half of the conversation, so the ticket has one."""
    response = portal.post(f"{TICKETS}/{ticket_id}/messages", json={"body": body})
    assert response.status_code == 201, response.text


def summarize(org: OrgSession, ticket_id: object) -> dict[str, Any]:
    response = org.post(summarize_path(ticket_id))
    assert response.status_code == 202, response.text
    return cast("dict[str, Any]", response.json())


def complete_summary(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: list[tuple[str, str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], str]:
    """Raise a ticket with a conversation, queue a summary, and run the worker for it.

    Needed by the cache-hit test, which is the one claim here that cannot be made from the
    request path alone: a *completed* summary has to exist before the route can answer with it.
    Returns the ticket and the completed row's id.
    """
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])
    queued = summarize(org, created["id"])

    provider = FakeProvider(SUMMARY)
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    ticket_id, organization_id, analysis_ids = queued_analyses[-1]
    assert ai_tasks.analyze_ticket(ticket_id, organization_id, analysis_ids) == {
        "completed": 1,
        "failed": 0,
        "skipped": 0,
    }

    return created, queued["id"]


# ---------------------------------------------------------------------------
# What the route answers
# ---------------------------------------------------------------------------


def test_summarizing_answers_202_with_one_pending_row_and_one_task(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: list[tuple[str, str, list[str]]],
) -> None:
    """`202` and a single row rather than `/analyze`'s list, because one operation was queued.

    The row carries `provider` and `model`, stamped at queue time — §41's "make it clear this
    is AI-generated" as data, and `AIAnalysis`'s own comment on why the stamp is taken now
    rather than read from config when the row is displayed.

    The queue is checked here too: one message, one analysis id, and that id is the row the
    response named. A route that answered with a row it had not queued would look identical
    from the response alone.
    """
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])

    row = summarize(org, created["id"])

    assert row["operation"] == "summarize"
    assert row["status"] == "pending"
    assert row["ticket_id"] == created["id"]
    settings = get_settings()
    assert row["provider"] == settings.AI_PROVIDER
    assert row["model"] == settings.AI_MODEL
    assert row["result"] is None

    # Two enqueues in total: the ticket's creation, and this. The last one names one row.
    assert len(queued_analyses) == 2
    queued_ticket, queued_org, analysis_ids = queued_analyses[-1]
    assert queued_ticket == created["id"]
    assert uuid.UUID(queued_org)
    assert analysis_ids == [row["id"]]


def test_a_current_summary_comes_back_completed_and_queues_nothing(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: list[tuple[str, str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§20's *"avoid regenerating"*, as the route's second outcome.

    The stored summary was completed and no message has arrived since, so it *is* the answer:
    the same row comes back, `completed`, with its text — and the broker hears nothing. This is
    the branch that makes the status code's indifference correct: both outcomes answer 202
    because both answer the question.

    The enqueue assertion counts the whole test, not the second request alone, so a route that
    queued a second summary and returned the *old* row by accident would be caught. The worker
    is run here rather than in the ledger, because "the archive is right" is not this file's
    claim — `tests/integration/test_ai_summary.py` measures the tokens and cost.
    """
    created, first_id = complete_summary(org, customer, portal, queued_analyses, monkeypatch)
    queued_before = len(queued_analyses)

    again = summarize(org, created["id"])

    assert again["id"] == first_id
    assert again["status"] == "completed"
    assert again["result"]["summary"] == SUMMARY["summary"]
    assert len(queued_analyses) == queued_before


# ---------------------------------------------------------------------------
# Who may reach it
# ---------------------------------------------------------------------------


def test_an_assigned_agent_may_summarize(
    org: OrgSession, customer: dict[str, Any], portal: OrgSession
) -> None:
    """`AI_REQUEST_ANALYSIS` is held by agents and managers — §3's matrix, one row.

    The agent has to be *assigned* for the row scope to include the ticket, which is the
    property `require_visible_ticket` is the only route to a `Ticket` for — see
    `app/repositories/ai_repository.py` on why the queries take the object rather than an id.
    """
    agent = org.add_user("agent", email="assigned@summaryapi.com")
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])
    assigned = org.post(
        f"{TICKETS}/{created['id']}/assign", json={"assigned_agent_id": agent.user_id}
    )
    assert assigned.status_code == 200, assigned.text

    response = agent.post(summarize_path(created["id"]))

    assert response.status_code == 202, response.text
    assert response.json()["operation"] == "summarize"


def test_a_customer_cannot_summarize_their_own_ticket(
    org: OrgSession, customer: dict[str, Any], portal: OrgSession
) -> None:
    """§3 gives the portal no AI access, and this is the test the wrong guard would fail.

    `TICKET_VIEW` is held by every portal account, so guarding this route with it — the obvious
    choice, since a ticket is what the route hangs off — would let a customer summarize a
    conversation containing staff-only notes they cannot read. Asserted on the §42 code rather
    than on the number alone, so a 403 from somewhere else in the stack cannot pass for this
    one.
    """
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])

    response = portal.post(summarize_path(created["id"]))

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_an_unassigned_agent_cannot_summarize_a_colleagues_ticket(
    org: OrgSession, customer: dict[str, Any], portal: OrgSession
) -> None:
    """The row scope applies to the summary by way of its ticket, and a 404 is the answer.

    An agent holds `AI_REQUEST_ANALYSIS`, so the capability check passes and the ticket
    resolution is the only thing standing in the way — which is exactly the ordering that makes
    this a test of the row scope rather than of the permission. A 404 and not a 403, for the
    reason ADR-009 gives: an agent must not be able to size the desk's workload by watching
    which ids are refused.
    """
    colleague = org.add_user("agent", email="colleague@summaryapi.com")
    outsider = org.add_user("agent", email="outsider@summaryapi.com")
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])
    org.post(f"{TICKETS}/{created['id']}/assign", json={"assigned_agent_id": colleague.user_id})

    refused = outsider.post(summarize_path(created["id"]))
    allowed = colleague.post(summarize_path(created["id"]))

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "TICKET_NOT_FOUND"
    assert allowed.status_code == 202, allowed.text


# ---------------------------------------------------------------------------
# The refusals that are not about permission
# ---------------------------------------------------------------------------


def test_a_ticket_in_another_tenant_is_a_404(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    register_org: Callable[..., OrgSession],
    queued_analyses: list[tuple[str, str, list[str]]],
) -> None:
    """A foreign ticket and a nonexistent one are the same answer, deliberately.

    The tenant predicate is in the query rather than in a comparison afterwards, so there is no
    path that could return a summary of another tenant's conversation — the row is never
    loaded. The second assertion is what makes this about isolation rather than about a 404:
    nothing was queued for the stranger's attempt.
    """
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])
    stranger = register_org(organization_name="Other Co")

    response = stranger.post(summarize_path(created["id"]))

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"
    assert len(queued_analyses) == 1


def test_a_ticket_that_does_not_exist_is_a_404(
    org: OrgSession, no_provider_is_reachable: FakeProvider
) -> None:
    """A uuid that names nothing. The same answer as a foreign one, and no provider either."""
    response = org.post(summarize_path(uuid.uuid4()))

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"
    assert no_provider_is_reachable.calls == 0


def test_an_unauthenticated_request_is_refused_before_anything_else(client: TestClient) -> None:
    """401 from the auth layer, which is what keeps §45's limit's subject the caller.

    The limiter and the capability dependency are declared together, so an anonymous request
    never reaches either — the same ordering ADR-014 takes for uploads, and the reason the rate
    limit's key can be a user id rather than an address.
    """
    response = client.post(summarize_path(uuid.uuid4()))

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


# ---------------------------------------------------------------------------
# §45 — the limit on the routes whose abuse is billed
# ---------------------------------------------------------------------------


def test_summarizing_is_rate_limited(
    org: OrgSession, customer: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route is wired to the limiter and renders 429 in the §42 envelope.

    Zero rather than a small number, for the reason `test_upload_is_rate_limited` gives: the
    assertion must not depend on how much an earlier test has counted against this key. No
    conversation is posted, which is deliberate — the limiter is a route dependency and so runs
    before the handler, and a request that reached the handler's empty-conversation guard would
    answer 422 instead. That the answer here is 429 is the ordering, stated.
    """
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_AI_PER_HOUR", 0, raising=False)
    created = org.add_ticket(customer["id"])

    response = org.post(summarize_path(created["id"]))

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.headers.get("Retry-After")
