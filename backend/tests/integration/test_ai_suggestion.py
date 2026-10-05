"""§21, end to end: the draft reaches the thread, and only staff can read it.

**This is Phase W's central claim, and it is the one file that can make it.** Everything
upstream is already covered: `tests/unit/test_ai_prompts.py` proves the text the model is
asked with, `tests/unit/test_ai_analysis_service.py` proves the reduction and the builders,
and `tests/api/test_ai_suggestion.py` proves the routes. None of those can prove that a
*worker*, in another process with its own session and no request behind it, loads the ticket
and the conversation people actually wrote, asks one question, writes an `ai_draft` row into
the thread **and leaves every column on the ticket alone** — which is what makes §21's
containment a property of the data rather than of a route's guard.

**The containment assertion is the customer's own read.** §21's *"AI must NEVER automatically
send a customer-facing response"* is enforced in several places, and only one of them is
observable from outside: the customer's portal session asks for their ticket's thread and does
not get the draft. Asserting on `sender_type` in the table would pass even if the visibility
filter were missing; asserting on the customer's response is the property that matters.

**The task is invoked as a function, not awaited**, for the reason
`tests/integration/test_ai_analysis.py` gives at length: `analyze_ticket`'s body is
`event_loop.run(_analyze(...))`, so calling it from a synchronous test runs the identical code
path a worker would, on a loop the task owns (ADR-011).

**§41's regenerate is a second request, and the trail is where that is visible.** Both requests
leave the ticket looking identical and both write a `pending` row, so the response cannot tell
them apart — deliberately, because asking again *is* regenerating. What can is the audit
trail: the first row says `ai_analysis_requested` and the second says
`ai_response_regenerated`.
"""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai.errors import AIPermanentError
from app.ai.fake import FakeProvider
from app.services import ai_service
from app.workers import ai_tasks
from tests.conftest import API, TICKETS, OrgSession

pytestmark = pytest.mark.integration

AUDIT = f"{API}/audit-logs"

#: The scripted answer to §21's call. Written against `SuggestedReply`, which has one field —
#: so `FakeProvider` would refuse a `confidence` here, which is the schema's rule showing up as
#: a test failure rather than as a `0.0` in the database.
DRAFT = {
    "body": (
        "Thanks for reporting this. Our file store is degraded and the platform team is "
        "replacing it, so the policy download should work again shortly."
    )
}

#: (ticket_id, organization_id, analysis_ids) as `enqueue_analysis` handed them over.
Queued = list[tuple[str, str, list[str]]]

#: How §21's rows are told apart from the ticket's own analysis. **Not by the action.** Raising
#: a ticket queues §18's classification and sentiment, and that request writes an
#: `ai_analysis_requested` row on the same ticket — so filtering on the action would count the
#: ticket's creation as a draft request. What is unique to a draft is the operation it names,
#: which `record_for` writes into `extra_data["operations"]` for both of §41's verbs.
_SUGGESTION = "suggest_response"


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the sibling integration files do."""


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Suggestion Co")


@pytest.fixture
def customer(org: OrgSession) -> dict[str, Any]:
    return org.add_customer(name="Ada Lovelace", email="ada@analytical.engine")


@pytest.fixture
def portal(org: OrgSession, customer: dict[str, Any]) -> OrgSession:
    """The customer's own login, so the containment test asks the right person.

    An admin session would see the draft — it holds `MESSAGE_READ_INTERNAL` — so a test that
    read the thread as staff and asserted the draft was absent would be asserting the opposite
    of the truth.
    """
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


def conversation(portal: OrgSession, org: OrgSession, ticket_id: object) -> None:
    """One customer message and one agent note — the smallest real conversation."""
    said = portal.post(f"{TICKETS}/{ticket_id}/messages", json={"body": "It will not download."})
    assert said.status_code == 201, said.text
    noted = org.post(f"{TICKETS}/{ticket_id}/notes", json={"body": "Their file store is degraded."})
    assert noted.status_code == 201, noted.text


def suggest(org: OrgSession, ticket_id: object) -> dict[str, Any]:
    """POST §21's route and return the row it answered with."""
    response = org.post(f"{TICKETS}/{ticket_id}/ai/suggest-response")
    assert response.status_code == 202, response.text
    return response.json()


def analyses(org: OrgSession, ticket_id: object) -> list[dict[str, Any]]:
    response = org.get(f"{TICKETS}/{ticket_id}/ai/analyses")
    assert response.status_code == 200, response.text
    return response.json()


def latest(org: OrgSession, ticket_id: object, operation: str) -> dict[str, Any]:
    matching = [row for row in analyses(org, ticket_id) if row["operation"] == operation]
    assert len(matching) == 1, f"expected one latest {operation} row, got {len(matching)}"
    return matching[0]


def drafts(engine: Engine, ticket_id: object) -> list[tuple[str, bool, str]]:
    """Every AI draft on a ticket: `(sender_type, is_internal, body)`.

    Read from the table rather than from the API, because the API is what the test above it
    is checking. This one is about what the worker actually wrote.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT CAST(sender_type AS text), is_internal, body FROM messages "
                "WHERE ticket_id = CAST(:id AS uuid) "
                "AND CAST(sender_type AS text) = 'ai_draft' ORDER BY created_at"
            ),
            {"id": str(ticket_id)},
        ).all()
    return [(str(row[0]), bool(row[1]), str(row[2])) for row in rows]


def ai_audit_actions(org: OrgSession, ticket_id: object) -> list[str]:
    """The §21 audit rows for a ticket, oldest first.

    Read through `/audit-logs` rather than out of the table, because `audit_logs` is the one
    table with no repository of its own — §34's rows are written from the transaction of the
    action they describe and read by the viewer, and a raw `SELECT` here would be the first
    thing in the suite to reach around both. The endpoint answers newest first; the tests below
    read the sequence, so this puts it back in the order it happened.
    """
    response = org.get(
        AUDIT,
        params={"target_type": "ticket", "target_id": str(ticket_id), "limit": 100},
    )
    assert response.status_code == 200, response.text
    matching = [
        row["action"]
        for row in response.json()
        if _SUGGESTION in row["extra_data"].get("operations", [])
    ]
    return list(reversed(matching))


def staged_notifications(engine: Engine, ticket_id: object) -> list[str]:
    """Every `ai_analysis_completed` notification for a ticket, as user ids."""
    with engine.connect() as conn:
        return [
            str(value)
            for value in conn.execute(
                text(
                    "SELECT user_id FROM notifications WHERE ticket_id = CAST(:id AS uuid) "
                    "AND CAST(notification_type AS text) = 'ai_analysis_completed'"
                ),
                {"id": str(ticket_id)},
            ).scalars()
        ]


def timeline(org: OrgSession, ticket_id: object) -> list[dict[str, Any]]:
    response = org.get(f"{TICKETS}/{ticket_id}/events")
    assert response.status_code == 200, response.text
    return [row for row in response.json() if row["event_type"] == "ai_analysis_completed"]


# ---------------------------------------------------------------------------
# The happy path — §21's "generate a draft"
# ---------------------------------------------------------------------------


def test_the_draft_completes_with_its_body_and_no_confidence(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§21's draft, as a row a staff screen can render.

    **The `None` is as much the assertion as the text is.** §41 says to show confidence where
    it is meaningful, and on a draft it is the least meaningful number in the system — a reply
    is edited rather than judged. `SuggestedReply` has no field for one and the column records
    its absence, exactly as §20's summary does. A dashboard averaging that column would
    otherwise report every draft as the least confident answer on the board.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])

    queued = suggest(org, created["id"])
    assert queued["operation"] == "suggest_response"
    assert queued["status"] == "pending"

    provider = use_provider(monkeypatch, DRAFT)
    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 0, "skipped": 0}
    assert provider.calls == 1

    row = latest(org, created["id"], "suggest_response")
    assert row["id"] == queued["id"]
    assert row["status"] == "completed"
    assert row["result"]["body"] == DRAFT["body"]
    assert row["confidence"] is None
    assert row["completed_at"] is not None
    assert row["prompt_tokens"] is not None


def test_the_worker_writes_one_internal_ai_draft_into_the_thread(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The draft is two records, and this is the second one.

    The `ai_analyses` row holds the model's own output, which the test above asserts. This one
    asserts the copy that makes §21 usable: a `messages` row with `sender_type = AI_DRAFT` and
    `is_internal` set, which `ai_draft_is_internal` (Phase D) refuses to store any other way.

    **`sender_user_id` is NULL**, because the model is not a user. Who asked for the draft is on
    the analysis row and in the audit trail; the draft itself was written by nobody in the
    tenant, and a foreign key pointing at whoever pressed the button would say otherwise every
    time a client rendered it as an author.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    suggest(org, created["id"])
    use_provider(monkeypatch, DRAFT)

    run(queued_analyses)

    assert drafts(sync_engine, created["id"]) == [("ai_draft", True, DRAFT["body"])]

    thread = org.get(f"{TICKETS}/{created['id']}/messages").json()
    posted = [row for row in thread if row["sender_type"] == "ai_draft"]
    assert len(posted) == 1
    assert posted[0]["body"] == DRAFT["body"]
    assert posted[0]["is_internal"] is True
    assert posted[0]["sender_user_id"] is None


def test_the_customer_cannot_see_the_draft(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§21's *"AI must NEVER automatically send a customer-facing response"*, observed.

    The draft exists, is in the ticket's thread, and is absent from the customer's read of that
    same thread — not redacted, not flagged, simply not there. That is `MESSAGE_READ_INTERNAL`
    deciding what a caller *sees* while `MessageRepository` applies the filter underneath, and
    it is the pair §21's containment actually rests on: a draft reaches an inbox when a person
    sends one through `accept`, and at no earlier moment.

    Asserting on the response rather than on `sender_type` in the table is the point. A filter
    that had gone missing would leave the row exactly where this test would still find it.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    suggest(org, created["id"])
    use_provider(monkeypatch, DRAFT)

    run(queued_analyses)

    thread = portal.get(f"{TICKETS}/{created['id']}/messages").json()

    assert [row for row in thread if row["sender_type"] == "ai_draft"] == []
    assert DRAFT["body"] not in [row["body"] for row in thread]
    # The customer's own message is still there, so this is a filter and not an empty list.
    assert "It will not download." in [row["body"] for row in thread]


def test_the_draft_changes_nothing_on_the_ticket(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§21 stores a draft; it does not apply one.

    Every field §18 and §19 write is untouched, and so is `tickets.priority` — the model was
    never asked for a classification on this call, and §6's separation between the
    recommendation and the business decision survives because no third writer exists. The
    row's own `result` and the `ai_draft` message are where a draft lives.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    before = org.get(f"{TICKETS}/{created['id']}").json()
    suggest(org, created["id"])
    use_provider(monkeypatch, DRAFT)

    run(queued_analyses)

    after = org.get(f"{TICKETS}/{created['id']}").json()
    for field in (
        "category",
        "subcategory",
        "sentiment",
        "sentiment_confidence",
        "ai_classification_confidence",
        "ai_recommended_priority",
        "priority",
        "status",
        "first_response_at",
    ):
        assert after[field] == before[field], field


def test_the_model_reads_the_ticket_and_then_the_conversation(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§21's two inputs, as a fact about the prompt.

    A draft made from the description alone would answer the question that opened the ticket
    and ignore everything said since. The internal note is in the prompt as well — unlike §20's
    summary, where including it is unambiguous, this is a judgement: the note is often where
    the explanation is, and the instruction tells the model it is context rather than something
    to repeat. The containment is that a person reviews the draft before sending it (§21) and
    the customer's read is checked above.
    """
    created = org.add_ticket(
        customer["id"],
        subject="Policy download fails",
        description="It fails in every browser.",
    )
    conversation(portal, org, created["id"])
    suggest(org, created["id"])
    provider = use_provider(monkeypatch, DRAFT)

    run(queued_analyses)

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert "Policy download fails" in request.content
    assert "It fails in every browser." in request.content
    assert "It will not download." in request.content
    assert "Their file store is degraded." in request.content
    assert "[agent (internal note)]" in request.content
    # The label is ours and names what the block can contain — `AIRequest`'s contract, restated
    # here because this is the one prompt whose answer a person can send onward without retyping
    # it. **Phase X widened it to name a third block**, the retrieved passages, and it names them
    # with "any" rather than "the" because most drafts have none; the content of a draft with
    # nothing retrieved is unchanged, which is what `test_ai_suggestion.py`'s sibling in
    # `tests/integration/test_knowledge_draft_grounding.py` asserts byte for byte.
    assert request.content_label == (
        "the customer's support ticket, the conversation so far, and any knowledge base "
        "passages retrieved for it"
    )


def test_a_ticket_nobody_has_replied_to_can_still_be_drafted_for(
    org: OrgSession,
    customer: dict[str, Any],
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§20 refuses an empty conversation; §21 must not, and the difference is the description.

    A summary of nothing is nothing, so `request_summary` raises. A draft of a freshly raised
    ticket is the case it is most useful for — the customer has just explained their problem and
    nobody has answered — and the material is the description, which `create_ticket` puts on the
    ticket and never on a message.
    """
    created = org.add_ticket(customer["id"], subject="Cannot log in")
    assert org.get(f"{TICKETS}/{created['id']}/messages").json() == []

    queued = suggest(org, created["id"])
    provider = use_provider(monkeypatch, DRAFT)

    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 0, "skipped": 0}
    assert len(provider.requests) == 1
    assert "Cannot log in" in provider.requests[0].content
    assert latest(org, created["id"], "suggest_response")["id"] == queued["id"]


# ---------------------------------------------------------------------------
# §41's regenerate, and §16's duplicate guard
# ---------------------------------------------------------------------------


def test_the_first_request_is_audited_as_requested_and_the_second_as_regenerated(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§41's *"regenerate"*, as the one thing that distinguishes it from the first draft.

    Both requests answer `202` with a `pending` row and both produce a completed draft, so
    nothing a client can read tells them apart — deliberately, because from the caller's point
    of view asking again *is* regenerating. The audit trail is where the two verbs are kept
    distinct, and it is the right place: §34 exists to record what happened, not to change what
    does.

    The second request also writes a second `ai_analyses` row and a second draft message, which
    is §6's *"keep previous versions"* rather than an overwrite.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    first = suggest(org, created["id"])
    use_provider(monkeypatch, DRAFT)
    run(queued_analyses)

    second = suggest(org, created["id"])

    assert second["id"] != first["id"]
    assert second["status"] == "pending"
    assert ai_audit_actions(org, created["id"]) == [
        "ai_analysis_requested",
        "ai_response_regenerated",
    ]

    # Both drafts are in the thread once the second has run, and neither replaced the other.
    use_provider(monkeypatch, DRAFT)
    run(queued_analyses)
    assert len(drafts(sync_engine, created["id"])) == 2


def test_a_draft_already_in_flight_is_not_queued_twice(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
) -> None:
    """§16's *"avoid duplicate processing"*, on the route a double-click is easiest on.

    The first request queues; the second finds a `SUGGEST_RESPONSE` row in flight and answers
    with it rather than queueing a second call for a draft already on its way — and, because
    nothing was queued, the second writes no audit row either. §53 names repeated AI calls as
    waste, and the draft is the second-most expensive one this system asks for.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])

    first = suggest(org, created["id"])
    second = suggest(org, created["id"])

    assert second["id"] == first["id"]
    assert second["status"] == "pending"
    # Exactly two enqueues: the ticket's creation, and the one draft. The second click added
    # nothing — and it is not a regeneration either, which is why no audit row follows it.
    assert len(queued_analyses) == 2
    assert ai_audit_actions(org, created["id"]) == ["ai_analysis_requested"]


# ---------------------------------------------------------------------------
# §18's last two steps, applied to a draft
# ---------------------------------------------------------------------------


def test_the_timeline_entry_names_the_draft_and_has_no_actor(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed draft is still a completion, so §18's step 7 still happens.

    One entry, naming `suggest_response` and nobody — a worker did this, after whoever asked
    had closed their browser.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    suggest(org, created["id"])
    use_provider(monkeypatch, DRAFT)

    run(queued_analyses)

    entries = timeline(org, created["id"])
    assert len(entries) == 1
    assert entries[0]["actor_user_id"] is None
    assert entries[0]["extra_data"]["operations"] == ["suggest_response"]


def test_a_draft_only_run_announces_without_alerting(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person who asked for the draft is the person about to read it.

    An `ai_analysis_completed` alert to the assignee exists because a classification changes
    the ticket and somebody should look. A draft changes nothing on the ticket and exists to be
    read by whoever asked for it, so the alert would interrupt that person about a page they
    are already on — the same reading Phase V took for §20's summary, and the reason
    `run_analysis`'s guard names `ANALYSIS_OPERATIONS` rather than every operation.
    """
    agent = org.add_user("agent", email="drafter@suggestionco.com")
    created = org.add_ticket(customer["id"])
    assigned = org.post(
        f"{TICKETS}/{created['id']}/assign", json={"assigned_agent_id": agent.user_id}
    )
    assert assigned.status_code == 200, assigned.text
    conversation(portal, org, created["id"])
    suggest(org, created["id"])
    use_provider(monkeypatch, DRAFT)

    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 0, "skipped": 0}
    assert staged_notifications(sync_engine, created["id"]) == []


def test_a_provider_failure_marks_the_row_and_writes_no_draft(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7's *"AI provider failure degrades gracefully"*, and the failure is contained.

    A failed draft is a `failed` row with a message staff can read and **no message in the
    thread** — the half that matters here, because an `ai_draft` written from a half-finished
    call would be a draft nobody could tell from a real one.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    queued = suggest(org, created["id"])
    use_provider(monkeypatch, AIPermanentError("the provider refused"))

    counts = run(queued_analyses)

    assert counts == {"completed": 0, "failed": 1, "skipped": 0}
    assert drafts(sync_engine, created["id"]) == []
    row = latest(org, created["id"], "suggest_response")
    assert row["id"] == queued["id"]
    assert row["status"] == "failed"
    assert row["error_message"]
    assert row["result"] is None
