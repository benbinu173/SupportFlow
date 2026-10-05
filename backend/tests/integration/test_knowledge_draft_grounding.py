"""§22's passages in §21's draft — and the two things a drafting feature must not do.

Phase X added a third block to §21's prompt, and a prompt change is easy to assert wrongly: a
test that only checks a passage reached the model would pass for a retrieval that fires on
every draft, and one that only checks the draft was written would pass for a retrieval that
never fires at all. So this file makes four separate claims about the *pair* of features, and
each is a claim about a different observable:

* **A retrieval hit grounds the draft** — the passage's own text is in the request
  `ai_service` sent, which is the only place "grounded" is checkable before the model turns it
  into prose.
* **An organization with nothing published makes no embedding call** — `calls == 0`, a number
  rather than a return value, because "we did not spend" is not a fact about an answer.
* **A retrieval failure still produces a draft** — the fail-open policy in
  `ai_analysis_service._knowledge_for_draft`, asserted on the `ai_draft` row rather than on a
  log line, because the point is that the agent still got a reply.
* **A hit stages exactly one `EMBED` ledger row** — and the row is charged to the ticket,
  unlike ingestion's embed rows, which have no ticket behind them.

**The worker is invoked as a function, not awaited**, for `test_ai_suggestion.py`'s reason:
`analyze_ticket`'s body is `event_loop.run(_analyze(...))`, so a synchronous test runs the
identical path a worker would, on a loop the task owns (ADR-011).

**The passages are written straight to the tables, with chosen vectors.** A hashed vector makes
similarity meaningless, so a retrieval that returned *something* would look the same as one that
returned the right thing. Each test below decides the question's vector and the passage's vector,
which is what lets the first test assert that this passage — and no other — was the one retrieved.
"""

import uuid
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai import prompts
from app.ai.errors import AIPermanentError
from app.ai.fake import FakeEmbeddingProvider, FakeProvider
from app.core.config import get_settings
from app.models.knowledge_chunk import EMBEDDING_DIMENSIONS
from app.services import ai_service
from app.workers import ai_tasks
from tests.conftest import TICKETS, OrgSession

pytestmark = pytest.mark.integration

#: §21's scripted answer. `SuggestedReply` has one field, so `FakeProvider` refuses anything
#: with a second — the schema's rule showing up as a test failure rather than as a stray column.
DRAFT = {
    "body": (
        "Thanks for getting in touch. Refunds are processed within five working days of approval."
    )
}

#: The passage the published document holds. Its wording is the assertion: a draft grounded in
#: it can only be grounded in it if this exact text was in the request.
REFUND_POLICY = "Refunds take five working days to reach a customer's account."

#: (ticket_id, organization_id, analysis_ids) as `enqueue_analysis` handed them over.
Queued = list[tuple[str, str, list[str]]]


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the sibling integration files do."""


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Grounding Co")


@pytest.fixture
def customer(org: OrgSession) -> dict[str, Any]:
    return org.add_customer(name="Grace Hopper", email="grace@grounding.co")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit(*weights: tuple[int, float]) -> list[float]:
    """A vector that is `weight` on each named axis and zero elsewhere.

    Cosine similarity ignores magnitude, so these are directions: giving the question and the
    passage the same `_unit((0, 1.0))` makes the passage a similarity-`1.0` neighbour by
    construction, which is what turns "the right passage was retrieved" into arithmetic.
    """
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for index, weight in weights:
        vector[index] = weight
    return vector


def _literal(vector: Sequence[float]) -> str:
    """A pgvector literal — `[1.0,0.0,…]` — for the one place a vector is written by hand."""
    return "[" + ",".join(repr(value) for value in vector) + "]"


def _organization(engine: Engine, session: OrgSession) -> str:
    """The organization id behind an authenticated session, read from the user it logged in as."""
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT organization_id FROM users WHERE id = CAST(:id AS uuid)"),
                {"id": str(session.user_id)},
            ).scalar_one()
        )


def publish(engine: Engine, organization_id: str, *, content: str, vector: Sequence[float]) -> str:
    """Write one published, completed document holding a single chunk with a chosen vector.

    Straight to the tables: ingestion's vectors come from a hash, and a drafting test needs the
    passage to be the question's nearest neighbour rather than a coincidence. The row is written
    as the worker leaves one — `completed`, `is_published`, a non-null embedding — because that
    is the triple `has_published_chunks` and `search_chunks` both require, and a test that got one
    of the three wrong would be testing the guard instead of the retrieval.

    Returns the document's id.
    """
    document_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_documents "
                "(id, organization_id, title, content, source_type, status, is_published, "
                " chunk_count) "
                "VALUES (CAST(:id AS uuid), CAST(:org AS uuid), 'Refund policy', :content, "
                " 'manual', 'completed', true, 1)"
            ),
            {"id": document_id, "org": organization_id, "content": content},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_chunks "
                "(id, organization_id, document_id, chunk_index, content, embedding, "
                " embedding_model, token_count) "
                "VALUES (CAST(:id AS uuid), CAST(:org AS uuid), CAST(:doc AS uuid), 0, "
                " :content, CAST(:embedding AS vector), 'text-embedding-3-small', 12)"
            ),
            {
                "id": str(uuid.uuid4()),
                "org": organization_id,
                "doc": document_id,
                "content": content,
                "embedding": _literal(vector),
            },
        )
    return document_id


def query_of(subject: str, description: str) -> str:
    """The string `_knowledge_for_draft` embeds for a ticket.

    Spelled out here rather than imported, because it is the contract between §21's draft path
    and §22's retrieval: the query is the ticket's own words, and a change to that shape should
    fail these tests rather than follow them.
    """
    return f"{subject}\n\n{description}"


class RefusingEmbedding(FakeEmbeddingProvider):
    """An embedding provider whose every call fails the way a vendor outage fails.

    `calls` is incremented in the override because `AIPermanentError` is raised before
    `super().generate_embedding` is reached — and the count of *attempts* is what the fail-open
    test wants, not the count of successful responses.
    """

    async def generate_embedding(self, texts: list[str]) -> Any:
        self.calls += 1
        self.texts.extend(texts)
        raise AIPermanentError("the embedding vendor refused")


def use_generation(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> FakeProvider:
    """Point §21's generation call at a scripted provider. This is the `_provider` seam."""
    provider = FakeProvider(*outcomes)
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


def script_embeddings(monkeypatch: pytest.MonkeyPatch, vectors: dict[str, list[float]]) -> None:
    """Point §22's retrieval at a scripted embedding provider — and at a provider that refuses
    an un-scripted text, so a question this file did not plan for is an `AssertionError` rather
    than a hashed vector that quietly retrieves the wrong thing."""
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: FakeEmbeddingProvider(vectors))


def run(queued: Queued) -> dict[str, int]:
    """Run the worker's task for the last thing that was queued, as the worker runs it."""
    ticket_id, organization_id, analysis_ids = queued[-1]
    return ai_tasks.analyze_ticket(ticket_id, organization_id, analysis_ids)


def suggest(org: OrgSession, ticket_id: object) -> dict[str, Any]:
    """POST §21's route and return the row it answered with."""
    response = org.post(f"{TICKETS}/{ticket_id}/ai/suggest-response")
    assert response.status_code == 202, response.text
    return response.json()


def drafts(engine: Engine, ticket_id: object) -> list[tuple[str, bool, str]]:
    """Every AI draft on a ticket: `(sender_type, is_internal, body)`, oldest first."""
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


def embed_rows(engine: Engine) -> list[dict[str, Any]]:
    """Every `EMBED` row the ledger holds, oldest first.

    Read from the table rather than the API, because §53's cost accounting has no read route —
    the ledger is written by the service and read by a report, and a test about the writing is
    the one case a raw `SELECT` is the right instrument.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT CAST(operation AS text) AS operation, provider, model, cost_usd, "
                "was_successful, user_id, ticket_id FROM ai_usage "
                "WHERE CAST(operation AS text) = 'embed' ORDER BY created_at"
            )
        ).all()
    return [dict(row._mapping) for row in rows]


# ---------------------------------------------------------------------------
# The hit — §22's passages reach §21's prompt
# ---------------------------------------------------------------------------


def test_the_draft_is_written_from_the_retrieved_passages(
    org: OrgSession,
    customer: dict[str, Any],
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§21's *"relevant knowledge"* step, as a fact about the prompt and about the draft.

    The passage is the question's nearest neighbour by construction — both carry `_unit((0,
    1.0))` — so a retrieval that returned nothing would fail the prompt assertion, and a
    retrieval that returned something else would fail it too. The draft's body is asserted
    separately, because a grounded prompt that produced no draft would be a plausible-looking
    pass.
    """
    subject, description = "Refund timing", "How long does a refund take to arrive?"
    created = org.add_ticket(customer["id"], subject=subject, description=description)
    publish(
        sync_engine,
        _organization(sync_engine, org),
        content=REFUND_POLICY,
        vector=_unit((0, 1.0)),
    )
    script_embeddings(monkeypatch, {query_of(subject, description): _unit((0, 1.0))})
    provider = use_generation(monkeypatch, DRAFT)

    suggest(org, created["id"])
    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 0, "skipped": 0}
    assert len(provider.requests) == 1
    content = provider.requests[0].content
    assert REFUND_POLICY in content
    # The ticket's own words are still the first block — the passage is added after them, not
    # instead of them.
    assert subject in content
    assert description in content
    assert drafts(sync_engine, created["id"]) == [("ai_draft", True, DRAFT["body"])]


def test_a_passage_below_the_threshold_does_not_reach_the_draft(
    org: OrgSession,
    customer: dict[str, Any],
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of "grounded", and the half a presence check would miss.

    A published passage orthogonal to the question — similarity `0.0`, under
    `RETRIEVAL_MIN_SIMILARITY` — is retrieved by nobody, so `_knowledge_for_draft` gets `[]` and
    the draft is built from the ticket alone. Without this test, a retrieval that ignored the
    threshold and handed every published passage to every draft would still pass the test above.
    """
    subject, description = "Refund timing", "How long does a refund take to arrive?"
    created = org.add_ticket(customer["id"], subject=subject, description=description)
    publish(
        sync_engine,
        _organization(sync_engine, org),
        content=REFUND_POLICY,
        vector=_unit((1, 1.0)),  # orthogonal to the question's direction
    )
    script_embeddings(monkeypatch, {query_of(subject, description): _unit((0, 1.0))})
    provider = use_generation(monkeypatch, DRAFT)

    suggest(org, created["id"])
    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 0, "skipped": 0}
    assert REFUND_POLICY not in provider.requests[0].content
    assert drafts(sync_engine, created["id"]) == [("ai_draft", True, DRAFT["body"])]


# ---------------------------------------------------------------------------
# The guard — nothing published means nothing spent
# ---------------------------------------------------------------------------


def test_an_organization_with_no_published_chunks_makes_no_embedding_call(
    org: OrgSession,
    customer: dict[str, Any],
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§53's *"repeated AI calls"*, refused at the one place a draft could start one.

    A tenant that has never opened the knowledge base has nothing to retrieve, and embedding
    their question to search an empty index spends their money to learn what one indexed read
    already said. The provider is the hashed one and its `calls` is the assertion — `0`, a
    number no return value can express — and the draft is still written, which is what makes
    this a guard rather than a feature that is off.
    """
    created = org.add_ticket(customer["id"])
    embeddings = FakeEmbeddingProvider()
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: embeddings)
    provider = use_generation(monkeypatch, DRAFT)

    suggest(org, created["id"])
    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 0, "skipped": 0}
    assert embeddings.calls == 0
    assert embed_rows(sync_engine) == []
    assert len(provider.requests) == 1
    assert drafts(sync_engine, created["id"]) == [("ai_draft", True, DRAFT["body"])]


# ---------------------------------------------------------------------------
# Fail-open — a knowledge outage must not stop a reply being written
# ---------------------------------------------------------------------------


def test_a_retrieval_failure_still_produces_a_draft(
    org: OrgSession,
    customer: dict[str, Any],
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7's *"AI provider failure degrades gracefully"*, at the seam where it is a policy.

    A published document exists, so the guard lets the retrieval through and the embedding call
    is really attempted — and it fails. The draft is still written from the ticket and the
    conversation, and the passage it could not retrieve is absent from the prompt: the failure
    is contained, not dressed up. Asserting on the `ai_draft` row rather than on a log line is
    the point, because what the policy promises is that the agent still got a reply.
    """
    subject, description = "Refund timing", "How long does a refund take to arrive?"
    created = org.add_ticket(customer["id"], subject=subject, description=description)
    publish(
        sync_engine,
        _organization(sync_engine, org),
        content=REFUND_POLICY,
        vector=_unit((0, 1.0)),
    )
    refusing = RefusingEmbedding()
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: refusing)
    provider = use_generation(monkeypatch, DRAFT)

    suggest(org, created["id"])
    counts = run(queued_analyses)

    assert counts == {"completed": 1, "failed": 0, "skipped": 0}
    assert refusing.calls == 1
    assert refusing.texts == [query_of(subject, description)]
    assert REFUND_POLICY not in provider.requests[0].content
    assert drafts(sync_engine, created["id"]) == [("ai_draft", True, DRAFT["body"])]


# ---------------------------------------------------------------------------
# The ledger — one row per retrieval, charged to the ticket
# ---------------------------------------------------------------------------


def test_one_retrieval_hit_stages_exactly_one_embed_ledger_row(
    org: OrgSession,
    customer: dict[str, Any],
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§53's *"track costs"*, on the call a draft adds to §21.

    One row, one provider, one price, and — the part that distinguishes this from ingestion's
    embed rows — a `ticket_id`. A draft's retrieval is paid for *because* a ticket asked for it,
    and `ticket_id` is the column that says so; an ingestion run has no ticket and leaves the
    same column NULL, which `tests/integration/test_knowledge_ingestion.py` asserts separately.
    `user_id` is NULL in both, because a worker spent this rather than a person.

    **The description is long on purpose.** `cost_usd` is a six-decimal column and the fake
    prices its prompt tokens by length, so a one-line question prices to `0.000000` and a
    `cost_usd > 0` assertion would fail for a reason that has nothing to do with pricing — the
    rounding floor, not a missing rate. A realistic paragraph of a customer's explanation puts
    the row above that floor, where the assertion is about the arithmetic again.
    """
    subject = "Refund timing"
    description = (
        "Our order arrived damaged and we have been trying to arrange a return for the last "
        "three weeks. The courier collected the parcel on the fourteenth, and nobody has told "
        "us when the refund will appear on the card we paid with. Could you confirm how long "
        "it normally takes once the return has been approved at your end?"
    )
    created = org.add_ticket(customer["id"], subject=subject, description=description)
    publish(
        sync_engine,
        _organization(sync_engine, org),
        content=REFUND_POLICY,
        vector=_unit((0, 1.0)),
    )
    script_embeddings(monkeypatch, {query_of(subject, description): _unit((0, 1.0))})
    use_generation(monkeypatch, DRAFT)

    suggest(org, created["id"])
    run(queued_analyses)

    rows = embed_rows(sync_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row["operation"] == "embed"
    assert row["provider"] == "fake"
    assert row["model"] == get_settings().EMBEDDING_MODEL
    assert row["was_successful"] is True
    assert row["cost_usd"] > 0
    assert row["user_id"] is None
    assert str(row["ticket_id"]) == str(created["id"])


def test_the_prompt_is_the_ticket_alone_when_there_is_nothing_to_retrieve(
    org: OrgSession,
    customer: dict[str, Any],
    queued_analyses: Queued,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase X's promise to Phase W, as a byte-level comparison rather than a claim.

    ADR-031 forecast that the knowledge block would be additive, and this is what "additive"
    means for the *content*: for a tenant with nothing published, the request §21 sends is what
    it sent before Phase X existed — the ticket block, and nothing appended. The content is
    compared against `prompts.ticket_content(subject, description)` rather than against the two
    fields joined by hand, because that is exactly what the draft path assembles: the ticket
    block comes from `prompts`, and what is under test is that nothing was added after it. A
    knowledge block that appeared unconditionally — an empty header, a placeholder — would fail
    here even though every row-based test above would pass.

    **The label is the one thing that did change, and it is asserted as such.** `_DRAFT_LABEL`
    names what the block *can* contain, so Phase X widened it to mention retrieved passages; it
    is a static description the model reads, not a report of what was retrieved, which is why it
    is the same on this call as on a grounded one. The content equality above is the property
    that matters, and the label is written out here so the widening is recorded rather than
    discovered by a failing assertion in another file.
    """
    subject, description = "Refund timing", "How long does a refund take to arrive?"
    created = org.add_ticket(customer["id"], subject=subject, description=description)
    embeddings = FakeEmbeddingProvider()
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: embeddings)
    provider = use_generation(monkeypatch, DRAFT)

    suggest(org, created["id"])
    run(queued_analyses)

    assert embeddings.calls == 0
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.content == prompts.ticket_content(subject, description)
    assert request.content_label == (
        "the customer's support ticket, the conversation so far, and any knowledge base "
        "passages retrieved for it"
    )
