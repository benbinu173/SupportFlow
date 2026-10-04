"""§36's third AI route and §41's accept — what they answer, who may reach them, what they cost.

**The route is where §16's *"the API should not wait unnecessarily for the LLM"* stays
observable for §21**, exactly as `tests/api/test_ai_summary.py` makes it observable for §20.
The autouse fixture below installs a provider with **no** scripted outcomes, so the first call
anywhere in this file raises; every request still answers, because a route that asks for a
draft queues work rather than doing it.

**The accept route is the one exception, and it is asserted rather than assumed.** It is the
only AI-shaped route in this API that calls no model at all — the draft it sends was counted
when it was asked for — and `test_accepting_makes_no_model_call` is what makes that a property
instead of a comment. It is also why the route carries no `limit_ai`: §45's limit exists to cap
the calls that are billed, and there is nothing here to cap.

**A customer is refused on both routes, and for §3's reason rather than a redaction one.** The
portal holds `TICKET_VIEW` and no AI capability at all, so `test_a_customer_cannot_ask_for_a_draft`
is the test that the obvious wrong guard — the ticket capability the path hangs off — would
fail. The visibility half is separate and is tested through the thread: the draft is an
`ai_draft` row that `MESSAGE_READ_INTERNAL` decides, and the accepted reply is a public one
nobody has to hold anything extra to read.
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

#: What the model writes. The reply an acceptance sends is deliberately different text, so a
#: test that read the wrong row would notice.
DRAFT = {"body": "Thanks for reporting this. Our file store is degraded and being replaced."}
SENT = "Thanks for reporting this — sorry for the trouble. We've found it on our side."

#: (ticket_id, organization_id, analysis_ids) as `enqueue_analysis` handed them over.
Queued = list[tuple[str, str, list[str]]]


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Suggestion API Co")


@pytest.fixture
def customer(org: OrgSession) -> dict[str, Any]:
    return org.add_customer(name="Grace Hopper", email="grace@navy.mil")


@pytest.fixture
def portal(org: OrgSession, customer: dict[str, Any]) -> OrgSession:
    return org.add_portal_user(customer["id"])


@pytest.fixture(autouse=True)
def no_provider_is_reachable(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    """A scripted provider with **no** outcomes, so any call at all is a hard failure.

    Autouse for `tests/api/test_ai_summary.py`'s reason: it turns "this request path does not
    call a model" from an inspection of the imports into a property every test here enforces.
    The two tests that have to *complete* a draft override it, deliberately and visibly.
    """
    provider = FakeProvider()
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def suggest_path(ticket_id: object) -> str:
    return f"{TICKETS}/{ticket_id}/{API_AI}/suggest-response"


def accept_path(ticket_id: object, draft_id: object) -> str:
    return f"{TICKETS}/{ticket_id}/{API_AI}/drafts/{draft_id}/accept"


def reply(portal: OrgSession, ticket_id: object, body: str = "It will not download.") -> None:
    """Post the customer's half of the conversation, so the ticket has one."""
    response = portal.post(f"{TICKETS}/{ticket_id}/messages", json={"body": body})
    assert response.status_code == 201, response.text


def suggest(org: OrgSession, ticket_id: object) -> dict[str, Any]:
    response = org.post(suggest_path(ticket_id))
    assert response.status_code == 202, response.text
    return cast("dict[str, Any]", response.json())


def thread(session: OrgSession, ticket_id: object) -> list[dict[str, Any]]:
    response = session.get(f"{TICKETS}/{ticket_id}/messages")
    assert response.status_code == 200, response.text
    return response.json()


def draft_in(session: OrgSession, ticket_id: object) -> dict[str, Any]:
    """The one `ai_draft` row a caller can see on this ticket, as that caller sees it."""
    drafts = [row for row in thread(session, ticket_id) if row["sender_type"] == "ai_draft"]
    assert len(drafts) == 1, f"expected one visible draft, got {len(drafts)}"
    return drafts[0]


def stage_draft(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Raise a ticket, give it a conversation, and let the worker draft a reply.

    Returns `(ticket, draft_row)`. The whole path is the real one, so the draft's id — which
    every acceptance test addresses the route with — is produced by the code under test rather
    than invented here.
    """
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])
    suggest(org, created["id"])

    provider = FakeProvider(DRAFT)
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    ticket_id, organization_id, analysis_ids = queued[-1]
    assert ai_tasks.analyze_ticket(ticket_id, organization_id, analysis_ids) == {
        "completed": 1,
        "failed": 0,
        "skipped": 0,
    }

    return created, draft_in(org, created["id"])


# ---------------------------------------------------------------------------
# What the suggestion route answers
# ---------------------------------------------------------------------------


def test_suggesting_answers_202_with_one_pending_row_and_one_task(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
) -> None:
    """`202` and a single row, because one operation was queued — `/summarize`'s shape.

    The row carries `provider` and `model` stamped at queue time, which is §41's *"make it clear
    this is AI-generated"* as data. **The draft itself is not in the response**: it arrives on
    the thread as an `ai_draft` message once the worker has run, and this is the first place
    §21's *"never automatically send"* is visible from outside — a request that answered with a
    reply would have sent one.

    The queue is checked too: two enqueues in total (the ticket's creation and this), and the
    last one names exactly the row the response did.
    """
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])

    row = suggest(org, created["id"])

    assert row["operation"] == "suggest_response"
    assert row["status"] == "pending"
    assert row["ticket_id"] == created["id"]
    settings = get_settings()
    assert row["provider"] == settings.AI_PROVIDER
    assert row["model"] == settings.AI_MODEL
    assert row["result"] is None
    # Nothing has been sent: the thread is the customer's own message and nothing else.
    assert [entry["sender_type"] for entry in thread(org, created["id"])] == ["customer"]

    assert len(queued_analyses) == 2
    queued_ticket, queued_org, analysis_ids = queued_analyses[-1]
    assert queued_ticket == created["id"]
    assert uuid.UUID(queued_org)
    assert analysis_ids == [row["id"]]


def test_the_latest_completed_draft_is_readable_from_the_analyses_route(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's own output, where every other operation's is read.

    §36 names no GET route for a draft, and none is needed: `GET /ai/analyses` already serves
    the latest row of each operation, so the text the model wrote is there beside the copy that
    went into the thread. The two are the same text and remain separate records — the row is
    the model's answer, the message is what the desk reads.
    """
    created, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    response = org.get(f"{TICKETS}/{created['id']}/{API_AI}/analyses")

    assert response.status_code == 200, response.text
    rows = [row for row in response.json() if row["operation"] == "suggest_response"]
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["result"]["body"] == DRAFT["body"]
    assert rows[0]["confidence"] is None
    # The thread copy agrees with the row, which is what makes the acceptance's before/after
    # a comparison of the model's text rather than of something the worker edited.
    assert draft["body"] == DRAFT["body"]
    assert draft["is_internal"] is True


# ---------------------------------------------------------------------------
# What the accept route answers
# ---------------------------------------------------------------------------


def test_accepting_answers_201_with_the_message_that_was_sent(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§41's accept, as the route that produces a customer-visible message.

    `201` rather than `202`, and the contrast is the point: `/suggest-response` answered 202
    because its work had not happened yet, and this one answers with a row that exists. What
    went out is the payload's text under the accepting agent's own authorship — not the model's
    text under the model's.
    """
    created, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    response = org.post(accept_path(created["id"], draft["id"]), json={"body": SENT})

    assert response.status_code == 201, response.text
    sent = response.json()
    assert sent["body"] == SENT
    assert sent["sender_type"] == "agent"
    assert sent["is_internal"] is False
    assert sent["sender_user_id"] == org.user_id
    assert sent["ticket_id"] == created["id"]


def test_accepting_makes_no_model_call(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    no_provider_is_reachable: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting sends text a person chose; there is nothing for a model to do.

    This is why the route carries no `limit_ai` — §45's limit caps the calls that are billed,
    and a route that makes none has nothing to cap. The provider is restored to the empty
    script before the request, so a call would raise rather than merely be counted.
    """
    created, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)
    monkeypatch.setattr(ai_service, "_provider", lambda: no_provider_is_reachable)

    response = org.post(accept_path(created["id"], draft["id"]), json={"body": SENT})

    assert response.status_code == 201, response.text
    assert no_provider_is_reachable.calls == 0


# ---------------------------------------------------------------------------
# Who may reach them
# ---------------------------------------------------------------------------


def test_an_assigned_agent_may_suggest_and_accept(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AI_REQUEST_SUGGESTION` and `MESSAGE_POST_REPLY` both reach an agent — §3's matrix.

    The agent has to be *assigned* for the row scope to include the ticket, which is the
    property `require_visible_ticket` is the only route to a `Ticket` for. The second half —
    that the same role may also send the result — is the pair the accept route declares, and an
    agent that could draft but not send would be the case that made the pair worth stating.

    Both requests are made **by the agent**, not by the administrator, so what is tested is the
    agent's own access rather than an administrator's ability to do it on their behalf.
    """
    agent = org.add_user("agent", email="assigned@suggestionapi.com")
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])
    assigned = org.post(
        f"{TICKETS}/{created['id']}/assign", json={"assigned_agent_id": agent.user_id}
    )
    assert assigned.status_code == 200, assigned.text

    queued = agent.post(suggest_path(created["id"]))
    assert queued.status_code == 202, queued.text

    provider = FakeProvider(DRAFT)
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    ticket_id, organization_id, analysis_ids = queued_analyses[-1]
    assert ai_tasks.analyze_ticket(ticket_id, organization_id, analysis_ids) == {
        "completed": 1,
        "failed": 0,
        "skipped": 0,
    }
    draft_id = draft_in(agent, created["id"])["id"]

    accepted = agent.post(accept_path(created["id"], draft_id), json={"body": SENT})

    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["sender_user_id"] == agent.user_id


def test_a_customer_cannot_ask_for_a_draft(
    org: OrgSession, customer: dict[str, Any], portal: OrgSession
) -> None:
    """§3 gives the portal no AI access, and this is the test the wrong guard would fail.

    `TICKET_VIEW` is held by every portal account, and a draft hangs off a ticket — so guarding
    this route with the ticket capability would let a customer make the desk's AI spend money
    on their behalf and read a draft written from the staff-only notes in their own thread.
    Asserted on the §42 code rather than on the number alone.
    """
    created = org.add_ticket(customer["id"])
    reply(portal, created["id"])

    response = portal.post(suggest_path(created["id"]))

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_a_customer_cannot_accept_a_draft(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The customer cannot even name the draft, and holding `MESSAGE_POST_REPLY` is not enough.

    A portal account may reply to their own ticket — that is `MESSAGE_POST_REPLY` and it is
    what makes the thread a conversation — so the capability that refuses this request is
    `AI_REQUEST_SUGGESTION`, the one §3's matrix gives the portal nowhere. The draft id is a
    real one, which is what makes this a permission refusal rather than the 404 a name that
    matched nothing would produce.
    """
    created, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    response = portal.post(accept_path(created["id"], draft["id"]), json={"body": SENT})

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"
    # Nothing was sent, so the refusal is the whole of the effect.
    assert SENT not in [entry["body"] for entry in thread(org, created["id"])]


# ---------------------------------------------------------------------------
# §21's distinction, through the thread endpoint
# ---------------------------------------------------------------------------


def test_the_draft_is_staff_only_and_the_accepted_reply_is_not(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same endpoint, two audiences — §21's *"explicit UI distinction"* as a fact.

    Before acceptance the desk's read of the thread contains the draft and the customer's does
    not. After it, both reads contain the reply and neither contains the draft. That is
    `MESSAGE_READ_INTERNAL` on the row scope and `sender_type` on the response, and it is why
    no separate draft endpoint exists: a client tells a draft from a reply by the field it is
    already given, and a client that cannot read the row never sees either.
    """
    created, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    staff_before = thread(org, created["id"])
    customer_before = thread(portal, created["id"])
    assert draft["id"] in [entry["id"] for entry in staff_before]
    assert [entry["id"] for entry in customer_before if entry["sender_type"] == "ai_draft"] == []

    assert org.post(accept_path(created["id"], draft["id"]), json={"body": SENT}).status_code == 201

    for session in (org, portal):
        rows = thread(session, created["id"])
        assert SENT in [entry["body"] for entry in rows]
    # The desk's read still holds the draft, and the customer's still does not — accepting
    # sends the text rather than publishing the row that offered it.
    assert draft["id"] in [entry["id"] for entry in thread(org, created["id"])]
    assert [
        entry for entry in thread(portal, created["id"]) if entry["sender_type"] == "ai_draft"
    ] == []


# ---------------------------------------------------------------------------
# §45 — the limit, on the route that has one and the route that does not
# ---------------------------------------------------------------------------


def test_suggesting_is_rate_limited(
    org: OrgSession, customer: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route is wired to the limiter and renders 429 in the §42 envelope.

    Zero rather than a small number, so the assertion does not depend on how much an earlier
    test counted against this key. No conversation is posted, which is deliberate: the limiter
    is a route dependency and runs before the handler, and §21's handler accepts an empty
    conversation anyway — the 429 is the limiter and nothing else.
    """
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_AI_PER_HOUR", 0, raising=False)
    created = org.add_ticket(customer["id"])

    response = org.post(suggest_path(created["id"]))

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.headers.get("Retry-After")


def test_accepting_is_not_rate_limited(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limit is set to zero and the acceptance still succeeds, which is the claim.

    A person sending a reply must not be told to wait because the desk's AI budget for the hour
    is spent — the text is already written and the model has already been paid for. Wired to
    `limit_ai`, this request would answer 429 and an agent would be unable to send a draft they
    were looking at.
    """
    created, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_AI_PER_HOUR", 0, raising=False)

    response = org.post(accept_path(created["id"], draft["id"]), json={"body": SENT})

    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# The refusals that are not about permission
# ---------------------------------------------------------------------------


def test_a_ticket_in_another_tenant_is_a_404_on_both_routes(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    register_org: Callable[..., OrgSession],
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign ticket and a nonexistent one are the same answer, deliberately.

    Both routes resolve the ticket before doing anything, so the tenant predicate is what
    refuses them: there is no path that could draft from another tenant's conversation or send
    into their thread. The enqueue count is what makes this about isolation rather than about a
    404 — the stranger's attempts queued nothing and sent nothing.
    """
    created, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)
    stranger = register_org(organization_name="Other Co")
    queued_before = len(queued_analyses)

    suggested = stranger.post(suggest_path(created["id"]))
    accepted = stranger.post(accept_path(created["id"], draft["id"]), json={"body": SENT})

    assert suggested.status_code == 404, suggested.text
    assert suggested.json()["error"]["code"] == "TICKET_NOT_FOUND"
    assert accepted.status_code == 404, accepted.text
    assert accepted.json()["error"]["code"] == "TICKET_NOT_FOUND"
    assert len(queued_analyses) == queued_before
    assert SENT not in [entry["body"] for entry in thread(org, created["id"])]


def test_a_ticket_that_does_not_exist_is_a_404(
    org: OrgSession, no_provider_is_reachable: FakeProvider
) -> None:
    """A uuid that names nothing, on both routes. No provider, and no queue entry either."""
    suggested = org.post(suggest_path(uuid.uuid4()))
    accepted = org.post(accept_path(uuid.uuid4(), uuid.uuid4()), json={"body": SENT})

    assert suggested.status_code == 404, suggested.text
    assert suggested.json()["error"]["code"] == "TICKET_NOT_FOUND"
    assert accepted.status_code == 404, accepted.text
    assert accepted.json()["error"]["code"] == "TICKET_NOT_FOUND"
    assert no_provider_is_reachable.calls == 0


def test_an_unauthenticated_request_is_refused_before_anything_else(client: TestClient) -> None:
    """401 from the auth layer, which is what keeps §45's limit's subject the caller.

    The limiter and the capability dependency are declared together, so an anonymous request
    never reaches either — the same ordering ADR-014 takes for uploads, and the reason the rate
    limit's key can be a user id rather than an address. Asserted on both routes, because the
    accept route declares its capabilities differently and a router added later is the one most
    likely to carry a different dependency.
    """
    ticket_id = uuid.uuid4()
    suggested = client.post(suggest_path(ticket_id))
    accepted = client.post(accept_path(ticket_id, uuid.uuid4()), json={"body": SENT})

    for response in (suggested, accepted):
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
