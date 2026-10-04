"""§18's pipeline, run for real: what the analysis writes, who it tells, and what it skips.

**This is Phase U's central claim, and it is the one file that can make it.** Everything
upstream is already covered: `tests/unit/test_ai_retry.py` proves the call path against a
recording session, `tests/unit/test_ai_prompts.py` proves the text the model is asked with,
and `tests/api/test_ai_analysis.py` proves the two routes. None of those can prove that a
*worker*, in another process with its own session and no request behind it, finds the rows it
was queued for, calls the provider twice, writes the ticket's columns, appends the timeline
entry with **no actor**, stages one notification for the assignee, and then — this is the one
worth the file — **does nothing at all the second time it is handed the same ids**.

**The task is invoked as a function, not awaited.** `analyze_ticket`'s body is
`event_loop.run(_analyze(...))`, so calling it from a synchronous test runs the identical
code path a worker would, on a loop the task owns (ADR-011) — the same argument
`tests/integration/test_sla_sweep.py` makes for its own task. Awaiting `run_analysis` directly
would run it inside `pytest-asyncio`'s loop instead, which is a different loop and not the one
a worker uses.

**The provider is scripted, and the script is in call order.** `FakeProvider` answers one
outcome per call, validated against the schema the *method* asks for, so the two entries below
are a classification and then a sentiment. That ordering is only meaningful because
`ai_repository.load_analyses` returns the rows in the enum's declaration order — see its
docstring; without the `ORDER BY` this file would be asserting something the database was free
to vary.

**The ticket is read back through the API; the ledger is read from the table.** The ticket's
fields are asserted on `GET /tickets/{id}` and the analysis rows on
`GET /tickets/{id}/ai/analyses`, because those are the shipped read paths and the claim worth
making is that the values are *visible*, not merely stored. The `ai_usage` and `notifications`
rows are read with SQL, for the reason `test_sla_sweep.py` gives about its own: an inbox
belongs to one authenticated session, and asking two users about one fact is two requests
where the rows are the fact. The tenant and the user on a ledger row appear on no response at
all — the token already says which tenant the caller is in.
"""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai.errors import AIPermanentError
from app.ai.fake import FakeProvider
from app.core.config import get_settings
from app.services import ai_service
from app.workers import ai_tasks
from tests.conftest import TICKETS, OrgSession

pytestmark = pytest.mark.integration

#: A tenant's analysis, answered by the scripted provider. The classification is §18's own
#: example with §51's band beside it; the sentiment is §19's, on the same ticket.
#:
#: The two are deliberately *different* answers — a customer reporting a billing fault is
#: negative and the ticket is high priority — so a test that mixed them up would fail rather
#: than pass by coincidence.
CLASSIFICATION = {
    "category": "Billing",
    "subcategory": "Duplicate Charge",
    "priority": "high",
    "confidence": 0.94,
}
SENTIMENT = {"sentiment": "negative", "confidence": 0.88}

#: (ticket_id, organization_id, analysis_ids) as `enqueue_analysis` handed them over.
Queued = list[tuple[str, str, list[str]]]

#: The sentence a failed row carries: `AIServiceError`'s default. Asserted as a literal
#: because the point is that the *provider's* text did not reach the row.
SERVICE_ERROR_MESSAGE = "The AI service is temporarily unavailable."


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file.

    `truncate_tables` is not autouse in the root conftest — requesting it pulls in a live
    Postgres, which is the property that keeps `tests/unit/` runnable with nothing started —
    so the opt-in is per file. Without it the suite's organizations accumulate and
    `register_org`'s counter hands two tests the same admin email.
    """


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Analysis Co")


@pytest.fixture
def customer(org: OrgSession) -> str:
    return str(org.add_customer(name="Ada Lovelace", email="ada@analytical.engine")["id"])


@pytest.fixture
def agent(org: OrgSession) -> OrgSession:
    """An agent, so a ticket can be assigned and the notification has a recipient."""
    return org.add_user("agent", email="agent@analysisco.com")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def use_provider(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> FakeProvider:
    """Point the service at a scripted provider. This is the seam `_provider` exists for."""
    provider = FakeProvider(*outcomes)
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


def run(queued: Queued) -> dict[str, int]:
    """Run the worker's task for the last thing that was queued, as the worker runs it.

    The task and not `run_analysis`: the task owns the event loop and the session factory, and
    it is what Celery calls. Calling it here exercises the real entry point including its
    `uuid.UUID` conversions, which is the part a test that awaited `run_analysis` directly
    would skip.
    """
    ticket_id, organization_id, analysis_ids = queued[-1]
    return ai_tasks.analyze_ticket(ticket_id, organization_id, analysis_ids)


def assign(org: OrgSession, ticket_id: object, agent: OrgSession) -> None:
    response = org.post(f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": agent.user_id})
    assert response.status_code == 200, response.text


def analyses(org: OrgSession, ticket_id: object) -> dict[str, dict]:
    """The ticket's latest analysis per operation, keyed by operation."""
    response = org.get(f"{TICKETS}/{ticket_id}/ai/analyses")
    assert response.status_code == 200, response.text
    return {row["operation"]: row for row in response.json()}


def ticket(org: OrgSession, ticket_id: object) -> dict:
    response = org.get(f"{TICKETS}/{ticket_id}")
    assert response.status_code == 200, response.text
    return response.json()


def timeline(org: OrgSession, ticket_id: object) -> list[dict]:
    response = org.get(f"{TICKETS}/{ticket_id}/events")
    assert response.status_code == 200, response.text
    return [row for row in response.json() if row["event_type"] == "ai_analysis_completed"]


def ledger_rows(engine: Engine, ticket_id: object) -> list[tuple[str, str | None]]:
    """Every `ai_usage` row for a ticket, as `(operation, user_id)`.

    Read from the table because the tenant and the user are what is being asserted, and neither
    appears on any response — the token already says which tenant the caller is in, and the
    ledger is not part of this API's read surface. `/analytics/overview` aggregates these rows
    and is what `tests/integration/test_ai_usage_ledger.py` covers.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT CAST(operation AS text), user_id, organization_id FROM ai_usage "
                "WHERE ticket_id = CAST(:id AS uuid) ORDER BY operation"
            ),
            {"id": str(ticket_id)},
        ).all()
    return [(str(row[0]), None if row[1] is None else str(row[1])) for row in rows]


def ledger_tenants(engine: Engine, ticket_id: object) -> set[str]:
    """The distinct organizations the ledger rows for a ticket name.

    Separate from `ledger_rows` so the tenant is asserted at the one test that is about the
    tenant, rather than widening a tuple every other caller would have to unpack.
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


def ticket_tenant(engine: Engine, ticket_id: object) -> str:
    """The tenant a ticket belongs to, read from the row the API just created.

    A ticket's `organization_id` is not on `TicketRead` — like every other read model here, the
    response omits it because the token already says which tenant the caller is in — so the
    test that checks the ledger's tenant has to get the expected value from somewhere, and the
    row is the honest source.
    """
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT organization_id FROM tickets WHERE id = CAST(:id AS uuid)"),
                {"id": str(ticket_id)},
            ).scalar_one()
        )


def staged_analysis_notifications(engine: Engine, ticket_id: object) -> list[tuple[str, str]]:
    """Every `ai_analysis_completed` notification for a ticket, as `(user_id, type)`."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT user_id, CAST(notification_type AS text) FROM notifications "
                "WHERE ticket_id = CAST(:id AS uuid) "
                "AND CAST(notification_type AS text) = 'ai_analysis_completed'"
            ),
            {"id": str(ticket_id)},
        ).all()
    return [(str(row[0]), str(row[1])) for row in rows]


# ---------------------------------------------------------------------------
# The happy path — §18's steps 3 to 7
# ---------------------------------------------------------------------------


def test_the_analysis_fills_the_ticket_and_its_two_rows(
    org: OrgSession,
    customer: str,
    agent: OrgSession,
    queued_analyses: Queued,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both operations run, and what they found is on the ticket the customer is looking at.

    The six fields §18 writes, read back through `GET /tickets/{id}`: category, subcategory, and
    the classification confidence; sentiment and its confidence; and the recommendation.
    Asserted through the API rather than the row because "the analysis reached the ticket" is
    the claim, and a value that is stored but not served has not reached anybody.
    """
    created = org.add_ticket(
        customer, subject="Charged twice", description="Order 88213 billed twice."
    )
    assign(org, created["id"], agent)
    use_provider(monkeypatch, CLASSIFICATION, SENTIMENT)

    counts = run(queued_analyses)

    assert counts == {"completed": 2, "failed": 0, "skipped": 0}

    read = ticket(org, created["id"])
    assert read["category"] == "Billing"
    assert read["subcategory"] == "Duplicate Charge"
    assert read["ai_classification_confidence"] == pytest.approx(0.94)
    assert read["sentiment"] == "negative"
    assert read["sentiment_confidence"] == pytest.approx(0.88)
    assert read["ai_recommended_priority"] == "high"

    rows = analyses(org, created["id"])
    assert sorted(rows) == ["classify", "sentiment"]
    assert rows["classify"]["status"] == "completed"
    assert rows["classify"]["result"]["category"] == "Billing"
    assert rows["classify"]["confidence"] == pytest.approx(0.94)
    assert rows["sentiment"]["result"]["sentiment"] == "negative"
    # Phase T's ledger columns, per row, which is what `AIResult` is threaded through for.
    assert rows["classify"]["prompt_tokens"] is not None
    assert rows["sentiment"]["prompt_tokens"] is not None
    assert rows["classify"]["completed_at"] is not None
    assert rows["sentiment"]["completed_at"] is not None


def test_the_recommendation_never_becomes_the_priority(
    org: OrgSession, customer: str, queued_analyses: Queued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6's separation, asserted on the one field that would break it silently.

    The model recommends `high`; the ticket was created at the default `medium` and must still
    read `medium` afterwards. This is not a technicality — §51's suggestion exists to be
    *compared* with the band a person chose, and a worker that applied its own suggestion would
    make every comparison agree by construction.

    The two columns sit beside each other on `TicketRead` for exactly this test to be able to
    state the difference.
    """
    created = org.add_ticket(customer)
    use_provider(monkeypatch, CLASSIFICATION, SENTIMENT)

    run(queued_analyses)

    read = ticket(org, created["id"])
    assert read["ai_recommended_priority"] == "high"
    assert read["priority"] == "medium"


def test_the_timeline_entry_has_no_actor(
    org: OrgSession, customer: str, queued_analyses: Queued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§18's step 7, and the `NULL` is the claim.

    Nobody performed this: a worker did, minutes after whoever raised the ticket closed their
    browser. `TicketEvent.actor_user_id`'s own comment describes the value — *"NULL when the
    system acted rather than a person — SLA breaches and completed AI analyses have no actor"* —
    and a fabricated id here would put an action in a user's history that they never took.

    One entry for two operations, which is §18's singular step and the reason both operations
    run in one task: two entries would read as two things having happened.
    """
    created = org.add_ticket(customer)
    use_provider(monkeypatch, CLASSIFICATION, SENTIMENT)

    run(queued_analyses)

    entries = timeline(org, created["id"])
    assert len(entries) == 1
    assert entries[0]["actor_user_id"] is None
    assert sorted(entries[0]["extra_data"]["operations"]) == ["classify", "sentiment"]


def test_the_ledger_is_charged_to_the_worker_s_tenant_with_no_user(
    org: OrgSession,
    customer: str,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two rows, both `NULL` on `user_id`, both on the ticket's organization.

    The `NULL` is `WorkerContext` made visible: a task has no authenticated user and does not
    invent one, so the ledger row says a machine spent this. The tenant is still recorded —
    otherwise `/analytics/overview` would report an AI cost that no tenant could see, which is
    the failure the context type exists to prevent.

    Two rows and not one, because a failed call is still spend and the ledger is per attempt;
    this run had one attempt per operation.

    The tenant is checked against the ticket's own row rather than assumed from the request,
    because a worker's tenant is the one value here that nothing on the response could catch
    being wrong.
    """
    created = org.add_ticket(customer)
    use_provider(monkeypatch, CLASSIFICATION, SENTIMENT)

    run(queued_analyses)

    assert ledger_rows(sync_engine, created["id"]) == [("classify", None), ("sentiment", None)]
    assert ledger_tenants(sync_engine, created["id"]) == {ticket_tenant(sync_engine, created["id"])}


def test_the_assignee_is_told_and_the_customer_is_not(
    org: OrgSession,
    customer: str,
    agent: OrgSession,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§26's "AI analysis completed", addressed to whoever will review it.

    §41 is why: model output is reviewed by a person before a customer sees any of it, so the
    alert goes to the agent working the ticket and never to the customer. An alert to the
    customer would be the first step of the automatic send §21 forbids.
    """
    created = org.add_ticket(customer)
    assign(org, created["id"], agent)
    use_provider(monkeypatch, CLASSIFICATION, SENTIMENT)

    run(queued_analyses)

    assert staged_analysis_notifications(sync_engine, created["id"]) == [
        (agent.user_id, "ai_analysis_completed")
    ]


def test_an_analysis_of_an_unassigned_ticket_tells_nobody(
    org: OrgSession,
    customer: str,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody is working it, so there is no one person to alert.

    The same reading `test_a_customer_reply_on_an_unassigned_ticket_notifies_nobody` makes, and
    the narrower of the two available: `notify_sla_alert` widens to managers because an
    unclaimed ticket past its deadline is the manager's problem, and an unclaimed ticket's
    analysis is not urgent at all — the result is waiting on the ticket for whoever takes it.
    Widening here would put a notification about every auto-analyzed ticket in the queue into
    every manager's inbox.

    The analysis itself still completes, which is the other half of the assertion: the
    notification is the part that has nobody, not the work.
    """
    created = org.add_ticket(customer)
    use_provider(monkeypatch, CLASSIFICATION, SENTIMENT)

    counts = run(queued_analyses)

    assert counts["completed"] == 2
    assert staged_analysis_notifications(sync_engine, created["id"]) == []


# ---------------------------------------------------------------------------
# Idempotency — §16's "avoid duplicate processing"
# ---------------------------------------------------------------------------


def test_a_redelivered_task_does_nothing(
    org: OrgSession, customer: str, queued_analyses: Queued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second delivery finds every row terminal and makes no call at all.

    This is what makes `task_acks_late` safe. The task runs under at-least-once delivery, so a
    worker killed after committing its results is handed the same message again — and the guard
    is the row status, not a lock: a row only ever leaves `pending` once, and `run_analysis`
    skips anything already terminal.

    **Asserted on the provider's call count**, because the return value alone cannot tell the
    two behaviours apart: a second run that re-asked the model and a second run that did nothing
    both leave the ticket looking correct. §53 names repeated AI calls as waste, and this is the
    number that would show it.
    """
    created = org.add_ticket(customer)
    provider = use_provider(monkeypatch, CLASSIFICATION, SENTIMENT)

    first = run(queued_analyses)
    second = run(queued_analyses)

    assert first == {"completed": 2, "failed": 0, "skipped": 0}
    assert second == {"completed": 0, "failed": 0, "skipped": 2}
    assert provider.calls == 2
    # One timeline entry, not two: the second run announced nothing because it did nothing,
    # which is the same rule as "nothing is announced when nothing succeeded".
    assert len(timeline(org, created["id"])) == 1


def test_a_ticket_deleted_before_the_run_is_not_an_error(
    org: OrgSession, customer: str, queued_analyses: Queued, sync_engine: Engine
) -> None:
    """A normal outcome, logged and returned rather than raised.

    `ai_analyses.ticket_id` is `ON DELETE CASCADE`, so the rows the task was handed are gone
    with the ticket and there is nothing left to report on. Raising would put the task back into
    a broker where a redelivery would fail identically, for a cause that will never change —
    which is the definition of a permanent error being treated as a transient one.

    Deleted with SQL rather than through a route: this API has no `DELETE /tickets/{id}`, and
    the state under test is "the row is gone when the task arrives", which a direct delete
    states exactly.
    """
    created = org.add_ticket(customer)
    ticket_id, organization_id, analysis_ids = queued_analyses[-1]

    with sync_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM tickets WHERE id = CAST(:id AS uuid)"), {"id": created["id"]}
        )

    assert ai_tasks.analyze_ticket(ticket_id, organization_id, analysis_ids) == {
        "completed": 0,
        "failed": 0,
        "skipped": 0,
    }


# ---------------------------------------------------------------------------
# One failure does not lose the other — §7
# ---------------------------------------------------------------------------


def test_a_permanent_failure_leaves_the_other_operation_completed(
    org: OrgSession, customer: str, queued_analyses: Queued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§7's *"AI provider failure degrades gracefully"*, as a row-level fact.

    The classification call succeeds and the sentiment call is refused. Both rows are terminal
    and they say different things, which is the shape that matters: a task that rolled back on
    the first failure would leave the good result nowhere, and one that marked every row failed
    would claim the model said nothing when it had answered.

    **The failed row carries `AIServiceError`'s sentence and not the provider's.** That sentence
    is a fixed string because `AIServiceError`'s docstring explains that an SDK error can quote
    the request, and the request carries the key in a header and the customer's words in the
    body. The row is shown to staff, so what is stored has to be the safe half — and the
    provider's text below deliberately spells something that looks like a key, so a row that
    passed it through would be caught here rather than in a log review.

    The ticket keeps the half that worked and gains nothing from the half that did not: a failed
    sentiment leaves `tickets.sentiment` NULL rather than defaulting it to `neutral`.
    """
    created = org.add_ticket(customer)
    use_provider(
        monkeypatch, CLASSIFICATION, AIPermanentError("the provider rejected key gsk_FAKEKEY")
    )

    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 1, "skipped": 0}

    rows = analyses(org, created["id"])
    assert rows["classify"]["status"] == "completed"
    assert rows["sentiment"]["status"] == "failed"
    assert rows["sentiment"]["error_message"] == SERVICE_ERROR_MESSAGE
    assert "gsk_FAKEKEY" not in rows["sentiment"]["error_message"]
    # No payload on a failure, and no tokens: the attempt's figures are on its ledger row, and
    # copying them here would make a row that produced nothing look like it produced something.
    assert rows["sentiment"]["result"] is None
    assert rows["sentiment"]["prompt_tokens"] is None

    read = ticket(org, created["id"])
    assert read["category"] == "Billing"
    assert read["sentiment"] is None


def test_a_total_failure_announces_nothing(
    org: OrgSession,
    customer: str,
    agent: OrgSession,
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§18's last two steps describe a *finished* analysis, and this was not one.

    With both operations refused there is no result to review and no change to the ticket, so a
    timeline entry saying "analysis completed" would be a false claim on the record and an alert
    would interrupt an agent about nothing. The rows say `failed` and `error_message` says why,
    which is where somebody asking "what happened to my analysis" should look.

    **The ledger rows are still committed.** `ai_service`'s docstring is explicit that a caller
    which lets its own rollback discard them *"loses exactly the record that matters most"* — a
    failed call is still spend, and `/analytics/overview`'s `failed_calls` is the number that
    makes it visible.
    """
    created = org.add_ticket(customer)
    assign(org, created["id"], agent)
    use_provider(
        monkeypatch,
        AIPermanentError("the configured model does not exist"),
        AIPermanentError("the configured model does not exist"),
    )

    counts = run(queued_analyses)

    assert counts == {"completed": 0, "failed": 2, "skipped": 0}
    assert timeline(org, created["id"]) == []
    assert staged_analysis_notifications(sync_engine, created["id"]) == []
    assert len(ledger_rows(sync_engine, created["id"])) == 2


# ---------------------------------------------------------------------------
# What the model is actually asked
# ---------------------------------------------------------------------------


def test_the_ticket_s_own_words_reach_the_provider(
    org: OrgSession, customer: str, queued_analyses: Queued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt is assembled from the ticket, in the builder's own format.

    What is asserted here is the service's half: the ticket's own words reach the provider as
    `content`, in the one format `prompts.ticket_content` produces, under a label that is
    ours. The fence is the *provider's* half — `content` is customer text and `content_label`
    is not, which is the contract `AIRequest` states — and it is asserted where it is applied,
    in `tests/unit/test_ai_groq.py`. `FakeProvider` records the request it was *handed* rather
    than the message a real provider builds from it, so the markers are not present here and a
    test that looked for them would only be testing the fake.
    """
    from app.ai import prompts

    org.add_ticket(customer, subject="Charged twice", description="Order 88213 was billed twice.")
    provider = use_provider(monkeypatch, CLASSIFICATION, SENTIMENT)

    run(queued_analyses)

    assert len(provider.requests) == 2
    for request in provider.requests:
        # Equality rather than a substring: this pins the builder, so a change to
        # `ticket_content` reaches this test instead of silently diverging from it.
        assert request.content == prompts.ticket_content(
            "Charged twice", "Order 88213 was billed twice."
        )
        # The label is ours, and it is not the ticket's subject.
        assert request.content_label == "the customer's support ticket"

    # Two different questions about the same ticket, so the two calls cannot be the same call: a
    # task that sent the classification instruction twice would still produce two rows and one of
    # them would be wrong.
    assert provider.requests[0].instruction != provider.requests[1].instruction


def test_the_row_records_the_model_that_was_asked(
    org: OrgSession, customer: str, queued_analyses: Queued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`provider` and `model` are stamped at queue time and survive into the row.

    §41's "make it clear this is AI-generated" as data rather than as a label a UI invents, and
    `AIAnalysis`'s own comment gives the other reason: config changes, and a historical analysis
    must still name the model that was actually asked. The assertion is against settings rather
    than a literal so a deployment's model change is not a test failure — what is being checked
    is that the stamp is the live configuration and not a constant.
    """
    created = org.add_ticket(customer)
    use_provider(monkeypatch, CLASSIFICATION, SENTIMENT)
    settings = get_settings()

    run(queued_analyses)

    for row in analyses(org, created["id"]).values():
        assert row["provider"] == settings.AI_PROVIDER
        assert row["model"] == settings.AI_MODEL


def test_the_queued_task_names_the_tenant_and_the_rows(
    org: OrgSession, customer: str, queued_analyses: Queued, sync_engine: Engine
) -> None:
    """What crosses the broker is three ids, and the tenant is one of them.

    A task is handed references rather than a payload, for the reason `ai_tasks` gives: a
    snapshot copied into the broker is stale by the time it runs. The organization id is the
    part that matters here — it is what the task builds its `WorkerContext` from, so it is the
    only thing standing between a worker and guessing which tenant it is acting for.

    Asserted on the *recorded* enqueue rather than on the task's signature, because the signature
    is what a reader can already see and the call site is where a wrong value would come from.
    `create_ticket` is what queues it, and this is the ticket it created.
    """
    created = org.add_ticket(customer)
    ticket_id, organization_id, analysis_ids = queued_analyses[-1]

    assert ticket_id == created["id"]
    assert organization_id == ticket_tenant(sync_engine, created["id"])
    # Two operations, and the ids are the rows `request_analysis` wrote before queueing — which
    # is what lets a client that reads back immediately see them as `pending`.
    assert len(analysis_ids) == 2
    assert all(uuid.UUID(value) for value in analysis_ids)
    assert list(analyses(org, created["id"])) == ["classify", "sentiment"]
