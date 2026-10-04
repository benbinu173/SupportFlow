"""§20, end to end: the summary is made from what people said, and only when it has to be.

**This is Phase V's central claim, and it is the one file that can make it.** Everything
upstream is already covered: `tests/unit/test_ai_prompts.py` proves the text the model is
asked with, `tests/unit/test_ai_analysis_service.py` proves the reduction and the
eligibility rule, and `tests/api/test_ai_summary.py` proves the route. None of those can
prove that a *worker*, in another process with its own session and no request behind it,
loads the conversation people actually wrote, asks one question about it, writes a row with
no confidence, changes nothing on the ticket — and then, on the next request, **does
nothing at all because the conversation had not moved**.

**The task is invoked as a function, not awaited**, for the reason
`tests/integration/test_ai_analysis.py` gives at length: `analyze_ticket`'s body is
`event_loop.run(_analyze(...))`, so calling it from a synchronous test runs the identical
code path a worker would, on a loop the task owns (ADR-011).

**The cache assertion is on the ledger, and it has to be.** A second request that returns
the stored summary and a second request that quietly re-asks the model both leave the ticket
looking correct and both return a `completed` row — the response cannot tell them apart, and
that is deliberate. What can is `ai_usage`: the cached path writes a row with `was_cached`
set and zero tokens, and `provider.calls` stays at one. §53 names repeated AI calls as waste
and §20 asks not to regenerate; this is the number that shows it.

**The conversation is posted over HTTP**, as a portal user's reply and an agent's internal
note, because that is the only way messages are created and a test that wrote them with SQL
would not be testing that the thing being summarized is what the API stores.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai.fake import FakeProvider
from app.models.enums import SenderType
from app.models.message import Message
from app.services import ai_service
from app.workers import ai_tasks
from tests.conftest import TICKETS, OrgSession

pytestmark = pytest.mark.integration

#: The scripted answer to §20's call. Written against `ConversationSummary`, which has one
#: field — so `FakeProvider` would refuse a `confidence` here, which is the schema's rule
#: showing up as a test failure rather than as a `0.0` in the database.
SUMMARY = {
    "summary": (
        "The customer has tried three times to download their policy document and it fails in "
        "every browser. Support reproduced it against the file store, which is degraded, and "
        "promised an update once the platform team has replaced it."
    )
}

#: (ticket_id, organization_id, analysis_ids) as `enqueue_analysis` handed them over.
Queued = list[tuple[str, str, list[str]]]


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the sibling integration files do."""


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Summary Co")


@pytest.fixture
def customer(org: OrgSession) -> dict[str, Any]:
    return org.add_customer(name="Ada Lovelace", email="ada@analytical.engine")


@pytest.fixture
def portal(org: OrgSession, customer: dict[str, Any]) -> OrgSession:
    """The customer's own login, so a customer message is a customer message.

    `post_reply` takes `sender_type` from the caller's role, so a reply posted by the admin
    session would be an *agent* message — which is eligible too, and would quietly make every
    test here pass without ever exercising the customer half of the conversation.
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
    """One customer message and one agent note — the smallest real conversation.

    The note is the half §20's reading decision is about: it is staff-only, it is often where
    the explanation lives, and `app/repositories/ai_repository.py` includes it on purpose
    because this route is gated by `AI_REQUEST_ANALYSIS` and never by `TICKET_VIEW`.
    """
    said = portal.post(f"{TICKETS}/{ticket_id}/messages", json={"body": "It will not download."})
    assert said.status_code == 201, said.text
    noted = org.post(f"{TICKETS}/{ticket_id}/notes", json={"body": "Their file store is degraded."})
    assert noted.status_code == 201, noted.text


def summarize(org: OrgSession, ticket_id: object) -> dict[str, Any]:
    """POST §20's route and return the row it answered with."""
    response = org.post(f"{TICKETS}/{ticket_id}/ai/summarize")
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


def usage_rows(engine: Engine, ticket_id: object) -> list[tuple[str, bool, int, int]]:
    """Every `ai_usage` row for a ticket: `(operation, was_cached, prompt, completion)`.

    Read from the table rather than through `/analytics/overview`, for the reason
    `test_ai_analysis.py` gives about its own ledger helper — the aggregate is covered where
    the aggregate lives, and what is being asserted here is the shape of the rows underneath
    it. `was_cached` is the column Phase T declared and nothing had ever set to `True`.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT CAST(operation AS text), was_cached, prompt_tokens, completion_tokens "
                "FROM ai_usage WHERE ticket_id = CAST(:id AS uuid) ORDER BY was_cached, operation"
            ),
            {"id": str(ticket_id)},
        ).all()
    return [(str(row[0]), bool(row[1]), int(row[2]), int(row[3])) for row in rows]


def staged_summary_notifications(engine: Engine, ticket_id: object) -> list[str]:
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


def insert_message(
    engine: Engine,
    ticket_id: object,
    sender_type: SenderType,
    body: str,
    *,
    is_internal: bool = False,
) -> None:
    """Append a message the API cannot post — a system entry, or an unsent AI draft.

    Neither of the two write routes can produce these `SenderType`s: a status change is not
    something a person posts, and §21's draft is Phase W. So the rows go in directly, in the
    shape the columns allow — `ai_draft_is_internal` requires `is_internal`, and
    `internal_note_not_from_customer` is why the system entry below is not marked internal.
    Built through the model's own table so the enum is serialized by the column's type rather
    than by a cast this test would have to keep in step with.
    """
    with engine.begin() as conn:
        organization_id = conn.execute(
            text("SELECT organization_id FROM tickets WHERE id = CAST(:id AS uuid)"),
            {"id": str(ticket_id)},
        ).scalar_one()
        conn.execute(
            Message.__table__.insert(),
            [
                {
                    "id": uuid.uuid4(),
                    "organization_id": organization_id,
                    "ticket_id": uuid.UUID(str(ticket_id)),
                    "sender_type": sender_type,
                    "body": body,
                    "is_internal": is_internal,
                }
            ],
        )


# ---------------------------------------------------------------------------
# The happy path — §20's first two sentences
# ---------------------------------------------------------------------------


def test_the_summary_completes_with_its_text_and_no_confidence(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§20's *"generate a concise summary"*, as a row a staff screen can render.

    **The `None` is as much the assertion as the text is.** §20 asks for a summary rather than
    a judgement, `ConversationSummary` has one field, and `ai_analyses.confidence` is nullable
    with a range check — so a summary row has no confidence rather than a confidence of zero.
    A dashboard averaging that column would otherwise report the summaries as the least
    confident answers on the board.
    """
    created = org.add_ticket(customer["id"], subject="Policy download fails")
    conversation(portal, org, created["id"])

    queued = summarize(org, created["id"])
    assert queued["operation"] == "summarize"
    assert queued["status"] == "pending"

    provider = use_provider(monkeypatch, SUMMARY)
    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 0, "skipped": 0}
    assert provider.calls == 1

    row = latest(org, created["id"], "summarize")
    assert row["id"] == queued["id"]
    assert row["status"] == "completed"
    assert row["result"]["summary"] == SUMMARY["summary"]
    assert row["confidence"] is None
    assert row["completed_at"] is not None
    assert row["prompt_tokens"] is not None


def test_the_model_reads_the_people_and_not_the_books(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§20's second half, as a fact about the prompt: who is in it and who is not.

    A summary is made from what people said. A `system` entry is a status change rather than
    anything anybody said, and an `ai_draft` is unsent — §21's *"AI must NEVER automatically
    send a customer-facing response"* made a data question, because a summary of what people
    said should not contain a draft nobody sent. Both rows are in the table and neither is in
    the prompt.

    The note **is** in the prompt, which is the other half of the decision: it is often where
    an agent writes down what was actually promised, and this route is gated by
    `AI_REQUEST_ANALYSIS` and never by `TICKET_VIEW`, so nobody who can read the summary is
    anybody who could not already read the note.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    insert_message(sync_engine, created["id"], SenderType.SYSTEM, "Status changed to pending.")
    insert_message(
        sync_engine,
        created["id"],
        SenderType.AI_DRAFT,
        "Draft reply that was never sent.",
        is_internal=True,
    )
    summarize(org, created["id"])
    provider = use_provider(monkeypatch, SUMMARY)

    run(queued_analyses)

    assert len(provider.requests) == 1
    prompt = provider.requests[0].content
    assert "It will not download." in prompt
    assert "Their file store is degraded." in prompt
    # The note is labelled as one, which is what the instruction tells the model to look for.
    assert "[agent (internal note)]" in prompt
    assert "Status changed to pending." not in prompt
    assert "Draft reply that was never sent." not in prompt
    # The label is ours and never the ticket's — `AIRequest`'s contract, restated here because
    # this is the longest block of customer text the system assembles.
    assert provider.requests[0].content_label == "the support conversation so far"


def test_the_summary_changes_nothing_on_the_ticket(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§20 stores a summary; it does not apply one.

    Every field §18 and §19 write is untouched, and so is `tickets.priority` — the model was
    never asked for a classification on this call and a summary that quietly moved a column
    would be the second writer §6's separation exists to prevent. The row's own `result` is
    where the summary lives, which is why no column on the ticket could hold it.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    before = org.get(f"{TICKETS}/{created['id']}").json()
    summarize(org, created["id"])
    use_provider(monkeypatch, SUMMARY)

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
    ):
        assert after[field] == before[field], field


def test_the_timeline_entry_names_the_summary_and_has_no_actor(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A summary completion is still a completion, so §18's step 7 still happens.

    One entry, naming `summarize` and nobody — a worker did this, minutes after whoever asked
    closed their browser. What is different from an analysis is the notification, which is the
    next test.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    summarize(org, created["id"])
    use_provider(monkeypatch, SUMMARY)

    run(queued_analyses)

    entries = timeline(org, created["id"])
    assert len(entries) == 1
    assert entries[0]["actor_user_id"] is None
    assert entries[0]["extra_data"]["operations"] == ["summarize"]


def test_a_summary_only_run_announces_without_alerting(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person who asked for the summary is the person reading it.

    An `ai_analysis_completed` alert to the assignee exists because a classification changes
    the ticket and somebody should look. A summary changes nothing and exists to be read by
    whoever asked for it, so the alert would interrupt that person about a page they are
    already on. The timeline entry and the socket event still happen — see the test above —
    and the run still completes.
    """
    agent = org.add_user("agent", email="looker@summaryco.com")
    created = org.add_ticket(customer["id"])
    assigned = org.post(
        f"{TICKETS}/{created['id']}/assign", json={"assigned_agent_id": agent.user_id}
    )
    assert assigned.status_code == 200, assigned.text
    conversation(portal, org, created["id"])
    summarize(org, created["id"])
    use_provider(monkeypatch, SUMMARY)

    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 0, "skipped": 0}
    assert staged_summary_notifications(sync_engine, created["id"]) == []


# ---------------------------------------------------------------------------
# §20's third and fourth sentences — the freshness check
# ---------------------------------------------------------------------------


def test_a_second_summary_with_nothing_new_returns_the_stored_one_for_free(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§20's *"avoid regenerating after every tiny message if unnecessary"*, measured.

    The request answers `202` with the **same row**, `completed`, and the broker hears nothing.
    Both halves matter: the row identity is what makes it the stored summary rather than a
    fresh one that happens to agree, and the enqueue count is what makes "no second call" a
    fact rather than an inference from the text.

    **The evidence that a call did not happen is the ledger.** `ai_usage` gains exactly one row
    with `was_cached` set, zero tokens and zero cost — the row Phase T declared for this and
    nothing had ever written. It is a row rather than a silence so the saving is a count beside
    the calls it saved, which is what `/analytics/overview`'s `cached_calls` reports.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    first = summarize(org, created["id"])
    provider = use_provider(monkeypatch, SUMMARY)
    run(queued_analyses)
    queued_before = len(queued_analyses)

    second = summarize(org, created["id"])

    assert second["id"] == first["id"]
    assert second["status"] == "completed"
    assert second["result"]["summary"] == SUMMARY["summary"]
    # Nothing was queued, no second row exists, and the model was asked exactly once.
    # `latest` asserts there is exactly one `summarize` row for this ticket, which is the
    # "no second row" half; that it is the *same* row is the other half.
    assert len(queued_analyses) == queued_before
    assert latest(org, created["id"], "summarize")["id"] == first["id"]
    assert provider.calls == 1

    rows = usage_rows(sync_engine, created["id"])
    # The real call, and the one nobody made.
    assert [(operation, cached) for operation, cached, _, _ in rows] == [
        ("summarize", False),
        ("summarize", True),
    ]
    assert [row for row in rows if row[1]] == [("summarize", True, 0, 0)]


def test_one_new_message_makes_the_summary_stale_again(
    org: OrgSession,
    customer: dict[str, Any],
    portal: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§20's *"allow regeneration when the conversation changes significantly"*, read literally.

    Any new message is a change: there is no threshold and no message-count rule, because the
    rule that actually prevents regenerating on every tiny message is that nothing runs until a
    person asks. Given a person asking, the honest answer is the current conversation rather
    than a refusal — and the completed row is not touched, so §6's comparison between the two
    summaries survives.
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])
    first = summarize(org, created["id"])
    use_provider(monkeypatch, SUMMARY)
    run(queued_analyses)
    queued_before = len(queued_analyses)

    said = portal.post(
        f"{TICKETS}/{created['id']}/messages", json={"body": "Tried again in Firefox, same error."}
    )
    assert said.status_code == 201, said.text

    again = summarize(org, created["id"])

    assert again["id"] != first["id"]
    assert again["status"] == "pending"
    assert len(queued_analyses) == queued_before + 1
    # Two summarize rows now — the new one is the latest, and §20's "store the latest" is that
    # reading rather than a column that overwrote the first.
    assert latest(org, created["id"], "summarize")["id"] == again["id"]


def test_a_summary_already_in_flight_is_not_queued_twice(
    org: OrgSession, customer: dict[str, Any], portal: OrgSession, queued_analyses: Queued
) -> None:
    """§16's *"avoid duplicate processing"*, on the route a double-click is easiest on.

    The first request queues; the second finds a `SUMMARIZE` row in flight and answers with it
    rather than queueing a second call for a question already on its way. Two clicks must not be
    two provider calls, which is §53's waste and the reason the guard is here rather than in the
    worker — the waste is avoided by not queueing, not by refusing to run.

    The classification and sentiment rows the ticket's creation queued are also in flight and
    deliberately do not count: `request_summary` filters to its own operation, because "a
    summary is being made" is not "an analysis is being made".
    """
    created = org.add_ticket(customer["id"])
    conversation(portal, org, created["id"])

    first = summarize(org, created["id"])
    second = summarize(org, created["id"])

    assert second["id"] == first["id"]
    assert second["status"] == "pending"
    # Exactly two: the ticket's creation, and the one summary. The second click added nothing.
    assert len(queued_analyses) == 2


def test_a_ticket_with_no_conversation_cannot_be_summarized(
    org: OrgSession, customer: dict[str, Any], queued_analyses: Queued
) -> None:
    """§20 is about *"long ticket conversations"*, and a fresh ticket has none.

    `create_ticket` writes the description onto the ticket and creates no message, so the
    conversation a summary is made from is genuinely empty until somebody replies. Asking
    anyway would send the model an empty block and spend a call to have it say so; §42's
    refusal says it here instead. `ValidationError` is the same answer `create_ticket` gives
    when a portal caller names somebody else's customer — a rule Pydantic cannot express.
    """
    created = org.add_ticket(customer["id"])

    response = org.post(f"{TICKETS}/{created['id']}/ai/summarize")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    # Nothing was created and nothing was queued — the refusal is the whole of the effect.
    assert [row for row in analyses(org, created["id"]) if row["operation"] == "summarize"] == []
    assert len(queued_analyses) == 1
