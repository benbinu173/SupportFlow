"""§41's *accept*, end to end: what went out, what stayed behind, and what was recorded.

**This file is about the second half of §21's containment.** `test_ai_suggestion.py` proves
that a draft reaches the thread and nobody outside the desk can read it. This one proves that
the only way a draft becomes customer-visible is a person sending it — and that sending it
leaves the draft exactly where it was rather than promoting it.

**Three claims, and each would survive a plausible wrong implementation:**

1. **The reply is a new message, not the draft relabelled.** A draft promoted in place
   (a `sender_type` update, an `is_internal` flip) would produce the same customer-visible
   thread, and it would also mutate a row Phase V's freshness watermark reads. The assertion
   that catches it is the draft's *own* row, read before and after: same body, same sender
   type, still internal.
2. **The edit is the payload's text, and the audit row compares it to what the model wrote.**
   §41 lists *edit* as a verb and there is no edit endpoint — the client sends the text it
   wants sent. So `before` and `after` are the two bodies, and `metadata["edited"]` is whether
   they differ. Both halves are asserted, because an implementation that always recorded
   `edited: true` would look right in the edited case alone.
3. **A draft id that does not name a draft on this ticket is a 404, and never a 403 or a
   422.** Three cases — another ticket, another tenant, a message that is not a draft — all
   resolve to the same answer, so a caller cannot use the response to tell a real message id
   in someone else's thread from an invented one (ADR-009).

**The draft is staged the real way**: a scripted provider, the worker's own entry point, and
the `ai_draft` row read back out of the thread. Writing the draft with SQL would make the
draft's shape an assumption of this test rather than a fact produced by the code under test,
and the body the audit row compares against comes from that row.
"""

from collections.abc import Callable
from typing import Any

import pytest

from app.ai.fake import FakeProvider
from app.services import ai_service
from app.workers import ai_tasks
from tests.conftest import API, TICKETS, OrgSession

pytestmark = pytest.mark.integration

AUDIT = f"{API}/audit-logs"

#: What the model wrote. `test_a_faithful_accept_records_edited_false` sends this back
#: unchanged and every other acceptance sends something else.
DRAFT_BODY = (
    "Thanks for reporting this. Our file store is degraded and the platform team is replacing "
    "it, so the policy download should work again shortly."
)

#: What an agent actually sent. Different from `DRAFT_BODY` by more than whitespace, because
#: `edited` is a comparison of bodies and a test that trimmed a space would not tell the two
#: implementations apart.
EDITED_BODY = (
    "Thanks for reporting this — sorry for the trouble. We've found the problem on our side "
    "and the policy download should work again within the hour."
)

#: (ticket_id, organization_id, analysis_ids) as `enqueue_analysis` handed them over.
Queued = list[tuple[str, str, list[str]]]


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the sibling integration files do."""


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Acceptance Co")


@pytest.fixture
def customer(org: OrgSession) -> dict[str, Any]:
    return org.add_customer(name="Ada Lovelace", email="ada@analytical.engine")


@pytest.fixture
def portal(org: OrgSession, customer: dict[str, Any]) -> OrgSession:
    """The customer's own login, so the visibility assertions are the customer's own read."""
    return org.add_portal_user(customer["id"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def use_provider(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> FakeProvider:
    """Point the service at a scripted provider. This is the seam `_provider` exists for."""
    provider = FakeProvider(*outcomes)
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


def run(queued: Queued) -> dict[str, int]:
    """Run the worker's task for the last thing that was queued, as the worker runs it."""
    ticket_id, organization_id, analysis_ids = queued[-1]
    return ai_tasks.analyze_ticket(ticket_id, organization_id, analysis_ids)


def messages(org: OrgSession, ticket_id: object) -> list[dict[str, Any]]:
    response = org.get(f"{TICKETS}/{ticket_id}/messages")
    assert response.status_code == 200, response.text
    return response.json()


def draft_in(org: OrgSession, ticket_id: object) -> dict[str, Any]:
    """The one `ai_draft` row on a ticket's thread, as staff read it."""
    drafts = [row for row in messages(org, ticket_id) if row["sender_type"] == "ai_draft"]
    assert len(drafts) == 1, f"expected one draft, got {len(drafts)}"
    return drafts[0]


def stage_draft(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, dict[str, Any]]:
    """Raise a ticket, give it a conversation, and let the worker draft a reply.

    Returns `(ticket_id, draft_row)`. The whole path is the real one — a portal reply, an
    agent note, the route, and `ai_tasks.analyze_ticket` — so the row this returns is the row
    the code under test produced rather than one this file constructed.
    """
    created = org.add_ticket(customer["id"])
    said = portal.post(
        f"{TICKETS}/{created['id']}/messages", json={"body": "It will not download."}
    )
    assert said.status_code == 201, said.text
    noted = org.post(
        f"{TICKETS}/{created['id']}/notes", json={"body": "Their file store is degraded."}
    )
    assert noted.status_code == 201, noted.text

    requested = org.post(f"{TICKETS}/{created['id']}/ai/suggest-response")
    assert requested.status_code == 202, requested.text

    use_provider(monkeypatch, {"body": DRAFT_BODY})
    assert run(queued) == {"completed": 1, "failed": 0, "skipped": 0}

    return created["id"], draft_in(org, created["id"])


def accept(org: OrgSession, ticket_id: object, draft_id: object, body: str) -> Any:
    """POST §41's accept with `body` as the text to send."""
    return org.post(
        f"{TICKETS}/{ticket_id}/ai/drafts/{draft_id}/accept",
        json={"body": body},
    )


def acceptances(org: OrgSession, ticket_id: object) -> list[dict[str, Any]]:
    """The `ai_response_accepted` rows for a ticket, oldest first.

    Read through `/audit-logs` for the reason `test_ai_suggestion.py`'s sibling gives:
    `audit_logs` has no repository, and §34's rows are written and read through the service
    and the viewer. The endpoint answers newest first, so this reverses.
    """
    response = org.get(
        AUDIT,
        params={"target_type": "ticket", "target_id": str(ticket_id), "limit": 100},
    )
    assert response.status_code == 200, response.text
    matching = [row for row in response.json() if row["action"] == "ai_response_accepted"]
    return list(reversed(matching))


# ---------------------------------------------------------------------------
# What goes out
# ---------------------------------------------------------------------------


def test_accepting_sends_the_text_as_the_agents_own_reply(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§41's *accept*, as the message the customer receives.

    **`sender_type` is `agent`, and that is the whole of §21's *"never make the user believe
    an AI suggestion was written by a human"* from the data's side.** A draft sent under
    `ai_draft` would tell the customer exactly what the desk would rather not advertise; a
    draft sent under `agent` is a reply the agent chose to send, which is what happened. The
    author is the person who pressed the button, not the model.
    """
    ticket_id, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    response = accept(org, ticket_id, draft["id"], EDITED_BODY)

    assert response.status_code == 201, response.text
    sent = response.json()
    assert sent["body"] == EDITED_BODY
    assert sent["sender_type"] == "agent"
    assert sent["is_internal"] is False
    assert sent["sender_user_id"] == org.user_id
    assert sent["ticket_id"] == str(ticket_id)


def test_the_customer_sees_the_reply_and_never_the_draft(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§21's containment, one step on from `test_ai_suggestion.py`.

    Before acceptance the customer's thread holds neither the draft nor its text. After, it
    holds the reply and **still** not the draft. The second half is the one worth asserting
    separately: an implementation that published the accepted message by flipping the draft's
    `is_internal` would pass the first check and fail this one only if the draft's *original*
    text is absent — which is why `EDITED_BODY` differs from `DRAFT_BODY` here.
    """
    ticket_id, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    before = portal.get(f"{TICKETS}/{ticket_id}/messages").json()
    assert DRAFT_BODY not in [row["body"] for row in before]
    assert [row for row in before if row["sender_type"] == "ai_draft"] == []

    assert accept(org, ticket_id, draft["id"], EDITED_BODY).status_code == 201

    after = portal.get(f"{TICKETS}/{ticket_id}/messages").json()
    bodies = [row["body"] for row in after]
    assert EDITED_BODY in bodies
    assert DRAFT_BODY not in bodies
    assert [row for row in after if row["sender_type"] == "ai_draft"] == []


def test_the_draft_row_itself_is_untouched(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting re-authors; it does not promote.

    The draft is read before acceptance and again after, and every field that could have been
    rewritten is compared. A promoted draft — the implementation `Message`'s docstring warns
    against — would pass every other test in this file, because the thread it produces is
    identical. What it would also do is mutate a row `_CONVERSATION_SENDERS` excludes from
    §20's summary, and Phase V's freshness watermark is built on messages not moving.
    """
    ticket_id, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)
    before = draft_in(org, ticket_id)

    assert accept(org, ticket_id, draft["id"], EDITED_BODY).status_code == 201

    after = draft_in(org, ticket_id)
    assert after["id"] == before["id"]
    for field in ("body", "sender_type", "sender_user_id", "is_internal", "created_at"):
        assert after[field] == before[field], field
    assert after["body"] == DRAFT_BODY
    assert after["is_internal"] is True
    # And the reply is a fourth row rather than the third one relabelled: the customer's
    # message, the agent's note, the draft, and what was sent.
    assert len(messages(org, ticket_id)) == 4


def test_accepting_the_same_draft_twice_sends_twice(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The draft is an offer, not a claim on the reply.

    §41 does not make a draft single-use, and enforcing that would need a column recording it
    had been used — a second place recording what the audit trail already records. An agent
    who wants to send the same words to two people, or to send and then resend, is doing
    something ordinary; each send is its own message and its own audit row.
    """
    ticket_id, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    first = accept(org, ticket_id, draft["id"], EDITED_BODY)
    second = accept(org, ticket_id, draft["id"], EDITED_BODY)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert len(acceptances(org, ticket_id)) == 2
    assert [row["body"] for row in messages(org, ticket_id)].count(EDITED_BODY) == 2


# ---------------------------------------------------------------------------
# §41's edit, as the audit row that records it
# ---------------------------------------------------------------------------


def test_an_edited_accept_records_both_bodies_and_that_they_differ(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§41's *edit*, which has no route and does not need one.

    The client sends the text it wants sent; whether that text is a rewrite is a comparison
    the server can make and the client cannot be trusted to report. §34's `before`/`after` is
    exactly the mechanism — its docstring asks for the pair *"where appropriate"*, and a
    draft and the reply it became is the case it was written for.

    `draft_id` is in the same row so the two ends can be joined back to the offer without
    matching on text, which two acceptances of the same draft would make ambiguous.
    """
    ticket_id, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    assert accept(org, ticket_id, draft["id"], EDITED_BODY).status_code == 201

    rows = acceptances(org, ticket_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_user_id"] == org.user_id
    assert row["extra_data"]["before"] == DRAFT_BODY
    assert row["extra_data"]["after"] == EDITED_BODY
    assert row["extra_data"]["edited"] is True
    assert row["extra_data"]["draft_id"] == draft["id"]


def test_a_faithful_accept_records_edited_false(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the comparison, and the reason the flag is computed rather than set.

    An implementation that wrote `edited: True` unconditionally would pass the test above and
    report every acceptance as a rewrite — which is the number §41's *"show confidence where
    it is meaningful"* would have a dashboard plot. Sending the draft back unchanged is the
    case that makes the flag mean something.
    """
    ticket_id, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    assert accept(org, ticket_id, draft["id"], DRAFT_BODY).status_code == 201

    row = acceptances(org, ticket_id)[0]
    assert row["extra_data"]["before"] == DRAFT_BODY
    assert row["extra_data"]["after"] == DRAFT_BODY
    assert row["extra_data"]["edited"] is False
    # The message that went out is the draft's text, unaltered.
    assert DRAFT_BODY in [row["body"] for row in messages(org, ticket_id)]


# ---------------------------------------------------------------------------
# A draft id that names nothing on this ticket — ADR-009
# ---------------------------------------------------------------------------


def test_a_draft_on_another_ticket_is_a_404(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The draft is resolved *on this ticket*, not by id across the organization.

    Both tickets are the same tenant's, so this is not an isolation test — it is a check that
    acceptance cannot be pointed at an offer made in a different conversation. Sending another
    thread's draft as this thread's reply is not a privilege escalation, but it is a reply
    whose on-record provenance is false, which is the thing the audit row would then be lying
    about.
    """
    mine = org.add_ticket(customer["id"])
    _, theirs = stage_draft(org, customer, portal, queued_analyses, monkeypatch)

    response = accept(org, mine["id"], theirs["id"], DRAFT_BODY)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "AI_DRAFT_NOT_FOUND"
    assert messages(org, mine["id"]) == []
    assert acceptances(org, mine["id"]) == []


def test_a_message_that_is_not_a_draft_is_a_404(
    org: OrgSession, customer: dict[str, Any], portal: OrgSession
) -> None:
    """A real message id, on the right ticket, is still not a draft.

    The answer is the same `404` as an invented id — deliberately, per ADR-009 — even though
    the caller here is an administrator of the tenant that owns the message. The endpoint's
    contract is *"accept this draft"*, and a reply is not a draft; the reply route is where a
    reply is sent. Distinguishing the two would make the response a way to probe whether a
    message id exists.
    """
    created = org.add_ticket(customer["id"])
    posted = portal.post(
        f"{TICKETS}/{created['id']}/messages", json={"body": "It will not download."}
    )
    assert posted.status_code == 201, posted.text

    response = accept(org, created["id"], posted.json()["id"], DRAFT_BODY)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "AI_DRAFT_NOT_FOUND"


def test_a_draft_from_another_tenant_is_a_404(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
    register_org: Callable[..., OrgSession],
) -> None:
    """The same request across a tenant boundary, which is `test_ai_isolation.py`'s property.

    The draft genuinely exists, so this is not the not-found path wearing a different name:
    `find_draft` scopes to the caller's organization as well as to the ticket, so another
    tenant's draft is invisible rather than forbidden. A `403` here would confirm that the id
    exists somewhere, which is the disclosure ADR-009 exists to prevent.
    """
    mine = org.add_ticket(customer["id"])
    other = register_org(organization_name="Rival Co")
    other_customer = other.add_customer(name="Grace Hopper", email="grace@rival.co")
    other_portal = other.add_portal_user(other_customer["id"])
    _, theirs = stage_draft(other, other_customer, other_portal, queued_analyses, monkeypatch)

    response = accept(org, mine["id"], theirs["id"], DRAFT_BODY)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "AI_DRAFT_NOT_FOUND"
    assert messages(org, mine["id"]) == []


def test_a_closed_ticket_refuses_acceptance(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting is a reply, so it inherits the reply's rules — including this one.

    The draft was offered while the ticket was open and the agent came back after it was
    closed. The refusal is not about the draft: a reply landing on a finished ticket is a
    message nobody is watching for, and the answer is to reopen it, which puts the ticket back
    in front of an agent. Nothing is written — no message, no audit row — because the refusal
    happens before either is staged.
    """
    ticket_id, draft = stage_draft(org, customer, portal, queued_analyses, monkeypatch)
    # `TICKET_TRANSITIONS` is a chain — open to assigned to in_progress to resolved to closed,
    # and `CLOSED` is reachable only from `RESOLVED`. So the ticket is walked to the end the way
    # a desk walks it, one request per edge, because `open` is never a legal target of `/status`.
    # The state the refusal below is tested against is therefore a state the API produced.
    agent = org.add_user("agent", email="closer@acceptanceco.com")
    assigned = org.post(f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": agent.user_id})
    assert assigned.status_code == 200, assigned.text
    for status in ("in_progress", "resolved"):
        moved = org.post(f"{TICKETS}/{ticket_id}/status", json={"status": status})
        assert moved.status_code == 200, moved.text
    closed = org.post(f"{TICKETS}/{ticket_id}/close")
    assert closed.status_code == 200, closed.text

    response = accept(org, ticket_id, draft["id"], EDITED_BODY)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    # Nothing went out: the text the agent wanted sent is nowhere, and the audit row that an
    # acceptance writes does not exist. The refusal happens before either is staged.
    assert acceptances(org, ticket_id) == []
    assert EDITED_BODY not in [row["body"] for row in messages(org, ticket_id)]
    # The draft is still where it was, which is what makes this a refusal rather than a
    # deletion — it is staff-only and internal, so the closed ticket did not disturb it.
    assert draft["id"] in [row["id"] for row in messages(org, ticket_id)]
    assert DRAFT_BODY in [row["body"] for row in messages(org, ticket_id)]
