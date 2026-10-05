"""§22's pipeline, run for real: what ingestion writes, what it refuses, and what it re-pays.

**This is the one file where a document stops being `pending`.** Everything upstream is covered:
`tests/unit/test_document_text.py` proves extraction, cleaning, and chunking as pure functions,
`tests/unit/test_ai_openai_embedding.py` proves the provider's HTTP mapping, `tests/api/
test_knowledge.py` proves the six routes and the task a create queues. None of those can prove
that *a worker, in another process with its own session and no request behind it* reads the
document it was queued for, flips it to `processing` **before** the first network call, embeds
each passage, writes one `knowledge_chunks` row per passage with a real 1536-dimension vector in
a real pgvector column, publishes the document, and — the assertions worth a file — does nothing
at all the second time it is handed the same ids.

**The task is invoked as a function, not awaited**, for the reason `tests/integration/
test_ai_analysis.py` gives at length: `ingest_document`'s body is `event_loop.run(_ingest(...))`,
so calling it from a synchronous test runs the identical code path a worker would, on a loop the
task owns (ADR-011). It also means the commits are **real** commits rather than the savepoints
the `db` fixture arranges, which is what lets `sync_engine` — a different connection — see the
`processing` state while the run is in flight. That is the only way the claim in `run_ingestion`'s
docstring ("the row is flipped before any network call, so running is a state another process can
see") can be checked at all.

**Documents are created through the API and ingested through the task**, which is the seam the
system actually has: `create_manual` commits a `pending` row and calls `enqueue_ingestion`, and
`queued_ingestions` (autouse, root conftest) records the pair a real broker would have carried.
Each test below hands that recorded pair to the task, so the wire format is exercised rather than
constructed.

**Nothing here mocks storage or the network.** A `manual` document's text is already in its row,
so the pipeline needs neither: the embedding provider is the scripted double and everything else
— the database, the pgvector column, the ledger, the audit trail — is real.
"""

from collections.abc import Callable, Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai.errors import AIPermanentError
from app.ai.fake import FakeEmbeddingProvider
from app.ai.provider import AIResult
from app.core.config import get_settings
from app.core.exceptions import AIServiceError
from app.schemas.knowledge import Embedding
from app.services import knowledge_service, url_fetch
from app.services.document_text import CHUNK_TARGET_TOKENS
from app.services.url_fetch import FetchedPage
from app.workers import knowledge_tasks
from tests.conftest import KNOWLEDGE, OrgSession

pytestmark = pytest.mark.integration

#: (document_id, organization_id) as `enqueue_ingestion` handed them over.
Queued = list[tuple[str, str]]

#: What an empty extraction is reported as. Imported from the module that raises it would be
#: tautological, so the assertion below checks the *shape* of the reason — it is this project's
#: own sentence and it names the cause — rather than a literal copied from the source.
_NO_TEXT = "no text could be extracted"

#: The sentence `AIServiceError` carries, and the point of that class: a provider that timed out
#: and a provider that answered with the wrong schema produce this one message, and the
#: provider's own text never reaches a row a person reads.
_PROVIDER_DOWN = AIServiceError.message

#: What `RefusingEmbeddings` fails with. It is a constant rather than a literal so the failing
#: test can assert its **absence** from the row — a string written inline at the raise site
#: would be invisible to the assertion, which is how a leak test comes to pass while leaking.
LEAKY_REASON = "must not leak: the provider's own words"


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the sibling integration files do."""


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Ingestion Co")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def use_embeddings(
    monkeypatch: pytest.MonkeyPatch, provider: FakeEmbeddingProvider | None = None
) -> FakeEmbeddingProvider:
    """Point the service at an embedding provider. This is the seam `_embedding_provider` is.

    The hashed mode is the default because these tests are about the *pipeline* and not about
    ranking: every text gets a stable, dimension-correct vector derived from its own bytes, so a
    chunk row has a real vector in it without a test having to script one per passage of a
    document the chunker decided the shape of.
    """
    provider = provider or FakeEmbeddingProvider()
    monkeypatch.setattr(knowledge_service.ai_service, "_embedding_provider", lambda: provider)
    return provider


def create(org: OrgSession, *, content: str, title: str = "Refund policy") -> str:
    """Register a manual document through the API and return its id."""
    response = org.post(KNOWLEDGE, json={"title": title, "content": content})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def ingest(queued: Queued) -> dict[str, str | int]:
    """Run the worker's task for the last thing that was queued, as the worker runs it."""
    document_id, organization_id = queued[-1]
    return knowledge_tasks.ingest_document(document_id, organization_id)


def paragraphs(count: int) -> str:
    """`count` paragraphs, each comfortably short of a chunk and long enough to be one alone.

    Both numbers matter. A paragraph below `CHUNK_TARGET_TOKENS` is a segment rather than
    something `_split_sentences` has to cut, and one large enough that two of them cannot share
    a chunk means the chunker's output is exactly `count` passages — which is what lets a test
    below assert a chunk *count* without restating the chunker's arithmetic.
    """
    sentence = "Refunds are processed within five working days of the request. "
    body = sentence * ((CHUNK_TARGET_TOKENS * 3) // len(sentence))
    return "\n\n".join(body for _ in range(count))


def document_row(engine: Engine, document_id: str) -> dict[str, object]:
    """One document's row, read from the table rather than from the route.

    From the table because the route is not what this file is testing: `run_ingestion` writes
    columns, and a claim about a column read through a schema would be a claim about the schema.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT CAST(status AS text) AS status, is_published, chunk_count, content, "
                "error_message, processed_at FROM knowledge_documents "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": document_id},
        ).one()
    return dict(row._mapping)


def chunks(engine: Engine, document_id: str) -> list[dict[str, object]]:
    """Every chunk of a document, in order, with its vector's length rather than the vector."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT chunk_index, content, embedding_model, token_count, "
                "vector_dims(embedding) AS width FROM knowledge_chunks "
                "WHERE document_id = CAST(:id AS uuid) ORDER BY chunk_index"
            ),
            {"id": document_id},
        ).all()
    return [dict(row._mapping) for row in rows]


def embeddings_in_ledger(engine: Engine) -> list[dict[str, object]]:
    """Every `EMBED` row the ledger holds, oldest first.

    Not scoped to a document: `ai_usage` has no document column, and each test below leaves
    exactly one document that could have spent. What is read is the *shape* of the spend —
    how many rows, under which model, at what cost, and whether it was charged to a ticket
    (an embedding job has no ticket, which is what `ticket_id IS NULL` records).
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT provider, model, prompt_tokens, completion_tokens, cost_usd, "
                "was_successful, was_cached, CAST(operation AS text) AS operation, user_id, "
                "ticket_id FROM ai_usage WHERE operation = 'embed' ORDER BY created_at"
            )
        ).all()
    return [dict(row._mapping) for row in rows]


class WatchingEmbeddings(FakeEmbeddingProvider):
    """An embedding provider that records the document's state as another process sees it.

    **This is the redelivery guarantee, caught in the act.** `run_ingestion`'s claim is that the
    row is committed as `processing` *before* the first network call, so a task that dies and is
    redelivered finds something to skip. Asserting on the row after the run proves only that it
    ended up `completed`; the property is about the order, and the order is only visible from
    outside the run. So the provider reads the row through `sync_engine` — a different connection
    from the worker's, so what it sees is what was *committed*, not what is in the session — at
    the moment the first embedding call is made.
    """

    def __init__(self, engine: Engine, document_id: str) -> None:
        super().__init__()
        self._engine = engine
        self._document_id = document_id
        #: The status the document was in when the provider was first called.
        self.status_at_first_call: object | None = None

    async def generate_embedding(self, texts: list[str]) -> AIResult[Embedding]:
        if self.status_at_first_call is None:
            self.status_at_first_call = document_row(self._engine, self._document_id)["status"]
        return await super().generate_embedding(texts)


class RefusingEmbeddings(FakeEmbeddingProvider):
    """Fails every call, with a string that must never reach a row a person reads.

    Subclassed from the same double the success path uses, so the two test cases differ in one
    method rather than in which kind of fake they run — and the distinctive text is a module
    constant the failing test asserts the *absence* of, rather than a literal built inside a
    monkeypatch call and invisible to the assertion.

    `calls` is incremented here rather than left to the base implementation, which counts inside
    its own body: a provider that raises before reaching it would report zero calls, and the
    count of *attempts* is exactly what a failure test wants to read.
    """

    async def generate_embedding(self, texts: Sequence[str]) -> AIResult[Embedding]:
        self.calls += 1
        raise AIPermanentError(LEAKY_REASON)


# ---------------------------------------------------------------------------
# The pipeline, end to end
# ---------------------------------------------------------------------------


def test_a_manual_document_is_ingested_and_published(
    org: OrgSession, queued_ingestions: Queued, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§22's six steps, and the row they leave behind.

    `pending` is where the create leaves it — asserted before the run, so the transition is a
    transition rather than a starting state nobody checked. Afterwards the document is
    `completed`, **published**, and carries the chunk count it actually wrote, and every chunk
    is a real row in a real pgvector column: one 1536-dimension vector each, the embedding
    model's name stamped on it, and a positive `token_count` (the column's own check constraint
    would refuse anything else, so this is the constraint being satisfied rather than a number
    being asserted for its own sake).
    """
    document_id = create(org, content=paragraphs(3))
    assert document_row(sync_engine, document_id)["status"] == "pending"

    embeddings = use_embeddings(monkeypatch)
    outcome = ingest(queued_ingestions)

    assert outcome == {"status": "completed", "chunks": 3}
    row = document_row(sync_engine, document_id)
    assert row["status"] == "completed"
    assert row["is_published"] is True
    assert row["chunk_count"] == 3
    assert row["error_message"] is None
    assert row["processed_at"] is not None
    # The retained text, cleaned — which for a manual document is what it was created with,
    # modulo the chunker's normalisation. The column is what a re-chunk would read.
    assert str(row["content"]).startswith("Refunds are processed")

    written = chunks(sync_engine, document_id)
    assert [chunk["chunk_index"] for chunk in written] == [0, 1, 2]
    assert {chunk["width"] for chunk in written} == {1536}
    assert {chunk["embedding_model"] for chunk in written} == {get_settings().EMBEDDING_MODEL}
    assert all(int(chunk["token_count"]) > 0 for chunk in written)
    # One call for one batch: `EMBEDDING_BATCH_SIZE` is sixty-four and this document has three
    # passages, so a second call would mean batching was not doing what it says.
    assert embeddings.calls == 1


def test_the_document_is_processing_before_the_first_embedding_call(
    org: OrgSession, queued_ingestions: Queued, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The claim that makes a redelivered task safe**, observed from outside the run.

    A worker that embedded first and wrote `processing` afterwards would look identical from
    every test that reads the row at the end, and would pay for the whole document twice
    whenever `task_acks_late` delivered it again after a crash. The status here is read through
    a second connection while the run is in flight, so it is the committed state rather than the
    session's — which is the difference between "the session knows it is processing" and "a
    process that restarts can find out".
    """
    document_id = create(org, content=paragraphs(2))
    watching = WatchingEmbeddings(sync_engine, document_id)
    use_embeddings(monkeypatch, watching)

    outcome = ingest(queued_ingestions)

    assert outcome["status"] == "completed"
    assert watching.status_at_first_call == "processing"


def test_a_redelivered_task_re_claims_nothing(
    org: OrgSession, queued_ingestions: Queued, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the claim above, and the one §16's *"avoid duplicate processing"* names.

    `acks_late` means at-least-once delivery, so the second run is not a hypothetical: it is what
    the broker does when the first run's acknowledgement is lost. The document is no longer
    `pending`, so `run_ingestion` skips it — **before** the embedding call, which is the part
    worth asserting. A skip implemented after embedding would cost the organization the whole
    document again and change nothing an end-of-test row would reveal.

    The chunk rows are checked too, because they are where the damage would land: one row per
    `(document_id, chunk_index)` is a unique constraint, so a second run that *did* proceed
    would fail rather than double-write — but it would fail after paying.
    """
    document_id = create(org, content=paragraphs(2))
    embeddings = use_embeddings(monkeypatch)

    first = ingest(queued_ingestions)
    assert first == {"status": "completed", "chunks": 2}
    after_first = chunks(sync_engine, document_id)
    calls_after_first = embeddings.calls

    second = ingest(queued_ingestions)

    assert second == {"status": "skipped", "chunks": 0}
    assert embeddings.calls == calls_after_first
    assert chunks(sync_engine, document_id) == after_first
    assert document_row(sync_engine, document_id)["chunk_count"] == 2


def test_a_document_deleted_before_its_task_runs_ingests_nothing(
    org: OrgSession, queued_ingestions: Queued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue-to-run window, and what a client does in it.

    A delete between the create and the worker is an ordinary outcome rather than an error: the
    chunks cascade with the document, so there is nothing left to fill. The task reports
    `missing`, embeds nothing, and does not raise into the broker — where a raise would be
    retried forever against a row that will never come back.

    This is also why the task takes references rather than a payload: the text it would have
    ingested is gone from the database, and a message carrying a copy of it would have embedded
    a document the tenant had deleted.
    """
    document_id = create(org, content=paragraphs(2))
    deleted = org.delete(f"{KNOWLEDGE}/{document_id}")
    assert deleted.status_code == 204, deleted.text

    embeddings = use_embeddings(monkeypatch)
    outcome = ingest(queued_ingestions)

    assert outcome == {"status": "missing", "chunks": 0}
    assert embeddings.calls == 0


def test_a_url_document_is_fetched_inside_the_worker_and_not_in_the_request(
    org: OrgSession, queued_ingestions: Queued, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16's *"the API should not wait unnecessarily"*, as a fact about where the request went.

    A URL document is registered `pending` with the client's URL as its reference and an empty
    body, and nothing is fetched until the task runs. The fetch is scripted here, so what is
    asserted is the wiring rather than the guard — `tests/unit/test_url_fetch.py` owns the guard
    — and the two things worth pinning are that the fetch happens **at ingestion** and that the
    page it returns is what gets extracted and embedded, not the URL.
    """
    page = FetchedPage(
        url="https://example.com/refunds",
        content_type="text/html",
        body=b"<html><body><p>Refunds take five working days.</p></body></html>",
    )
    fetched: list[str] = []

    async def fake_fetch(url: str) -> FetchedPage:
        fetched.append(url)
        return page

    monkeypatch.setattr(url_fetch, "fetch", fake_fetch)
    created = org.post(
        KNOWLEDGE, json={"title": "Refund policy", "url": "https://example.com/refunds"}
    )
    assert created.status_code == 201, created.text
    document_id = str(created.json()["id"])
    # The request is over and the network has not been touched: the fetch belongs to the worker.
    assert fetched == []
    assert document_row(sync_engine, document_id)["content"] == ""

    embeddings = use_embeddings(monkeypatch)
    outcome = ingest(queued_ingestions)

    assert outcome == {"status": "completed", "chunks": 1}
    assert fetched == ["https://example.com/refunds"]
    assert embeddings.texts == ["Refunds take five working days."]
    row = document_row(sync_engine, document_id)
    assert row["is_published"] is True
    assert row["content"] == "Refunds take five working days."


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


def test_a_failing_provider_fails_the_document_and_writes_no_chunks(
    org: OrgSession, queued_ingestions: Queued, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§7's *"AI provider failure degrades gracefully"*, and the shape of graceful here.

    A `failed` document with a reason an admin can read and **no chunks at all** — the half that
    matters, because a document that was indexed halfway answers nothing past the cut and
    nothing would say so. `is_published` is false beside it, so retrieval cannot see a partial
    document even if one were left behind.

    **The reason is this project's sentence, not the provider's.** `AIServiceError` carries one
    fixed message for every provider failure precisely so that an SDK error — which can quote
    the request, and the request carries the API key in a header — never reaches a field a
    person reads. The provider here fails with a distinctive string, and that string must not
    appear on the row.

    The ledger row is asserted as well, and its *shape* is the point: a failed call is recorded
    rather than dropped, so `/analytics` can show a failure that would otherwise be invisible —
    and it costs zero, because a request the vendor refused never reached a model and
    `AIError`'s token counts default to zero for exactly that case. `run_ingestion` commits it
    with the failure, which is `run_analysis`'s *"the ledger rows are committed even when every
    operation failed"*.
    """
    document_id = create(org, content=paragraphs(2))
    embeddings = use_embeddings(monkeypatch, RefusingEmbeddings())

    outcome = ingest(queued_ingestions)

    assert outcome == {"status": "failed", "chunks": 0}
    assert embeddings.calls == 1
    row = document_row(sync_engine, document_id)
    assert row["status"] == "failed"
    assert row["is_published"] is False
    assert row["chunk_count"] == 0
    assert row["error_message"] == _PROVIDER_DOWN
    assert LEAKY_REASON not in str(row["error_message"])
    assert chunks(sync_engine, document_id) == []

    ledger = embeddings_in_ledger(sync_engine)
    assert len(ledger) == 1
    assert ledger[0]["was_successful"] is False
    assert ledger[0]["prompt_tokens"] == 0
    assert ledger[0]["cost_usd"] == 0


def test_a_document_that_yields_no_text_fails_rather_than_completing_empty(
    org: OrgSession, queued_ingestions: Queued, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scanned PDF's outcome, reached here through a document that is only whitespace.

    The alternative — a `completed` document with no chunks — is the one failure this pipeline
    refuses to produce quietly: it would answer nothing, look ingested in every list, and give
    an admin no reason to re-upload. So `extract` raises rather than returning `""`, and the
    reason names the cause rather than the step.

    Nothing is embedded. The refusal happens at extraction, which is before the first call, so
    an unusable document costs nothing — and the check that no `EMBED` row exists is how that
    is stated.
    """
    document_id = create(org, content="   \n\n   ")
    embeddings = use_embeddings(monkeypatch)

    outcome = ingest(queued_ingestions)

    assert outcome == {"status": "failed", "chunks": 0}
    row = document_row(sync_engine, document_id)
    assert row["status"] == "failed"
    assert _NO_TEXT in str(row["error_message"])
    assert row["is_published"] is False
    assert chunks(sync_engine, document_id) == []
    assert embeddings.calls == 0
    assert embeddings_in_ledger(sync_engine) == []


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def test_the_ledger_holds_one_embed_row_per_batch_with_a_real_cost(
    org: OrgSession, queued_ingestions: Queued, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§17's cost attribution, on the one call the vendor bills by input alone.

    Five passages with the batch size cut to two is three calls, and the assertion is that the
    ledger holds **three rows** rather than one or five: a batch is one request to a vendor and
    one line of spend, which is the unit `ai_service.embed_texts` documents. The batch size is
    patched rather than a five-hundred-chunk document being generated, because the branch is one
    `range` call and the interesting claim is how many rows it produces.

    Every row is charged under `EMBEDDING_MODEL` — not `AI_MODEL`, which is the reason `_run`
    grew a `model` parameter — carries `completion_tokens = 0` (nothing was generated, a fact
    rather than a missing count), and costs a non-zero amount. `cost_usd` is priced from the
    table at the moment of the call, so a zero here would mean the embedding model had no rate.
    """
    document_id = create(org, content=paragraphs(5))
    embeddings = use_embeddings(monkeypatch)
    monkeypatch.setattr(knowledge_service, "EMBEDDING_BATCH_SIZE", 2)

    outcome = ingest(queued_ingestions)

    assert outcome == {"status": "completed", "chunks": 5}
    assert embeddings.calls == 3

    ledger = embeddings_in_ledger(sync_engine)
    assert len(ledger) == 3
    assert {row["model"] for row in ledger} == {get_settings().EMBEDDING_MODEL}
    assert {row["provider"] for row in ledger} == {"fake"}
    assert all(row["was_successful"] is True for row in ledger)
    assert all(row["was_cached"] is False for row in ledger)
    assert all(int(row["completion_tokens"]) == 0 for row in ledger)
    assert all(int(row["prompt_tokens"]) > 0 for row in ledger)
    assert all(row["cost_usd"] > 0 for row in ledger)
    # An ingestion has no ticket and no user: it is a background job, and the ledger's two
    # attribution columns are nullable for exactly this reason.
    assert {row["ticket_id"] for row in ledger} == {None}
    assert {row["user_id"] for row in ledger} == {None}
    assert chunks(sync_engine, document_id) != []


def test_a_document_over_the_chunk_cap_is_refused_rather_than_truncated(
    org: OrgSession, queued_ingestions: Queued, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§53's ceiling, and why reaching it is a failure rather than a stop.

    A document silently indexed halfway answers nothing past the cut and gives no sign that it
    did — the reason is on the row, which is where an admin looks. The cap is patched down
    rather than a five-hundred-page manual being generated: the check is one comparison, and
    what is worth asserting is that it fails the document and embeds none of it.
    """
    document_id = create(org, content=paragraphs(3))
    embeddings = use_embeddings(monkeypatch)
    monkeypatch.setattr(knowledge_service.document_text, "MAX_CHUNKS", 2)

    outcome = ingest(queued_ingestions)

    assert outcome == {"status": "failed", "chunks": 0}
    row = document_row(sync_engine, document_id)
    assert row["status"] == "failed"
    assert "more than the 2" in str(row["error_message"])
    assert chunks(sync_engine, document_id) == []
    # The refusal is `chunk`'s, which runs before `_embed` — so the cap costs nothing.
    assert embeddings.calls == 0
