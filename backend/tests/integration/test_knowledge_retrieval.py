"""§23's retrieval and §24's refusal, against a real pgvector column.

**This file is about the two decisions retrieval makes**, and both are made in SQL: which
passages are eligible (`is_published AND status = completed`) and which are close enough
(`distance <= 1 - RETRIEVAL_MIN_SIMILARITY`). `tests/api/test_knowledge.py` drives the question
route with `knowledge_service.retrieve` replaced by a stub, which proves the route and the
citation mapping and can prove nothing about either decision. Here the query runs.

**The vectors are written by hand, and that is the whole method.** A retrieval test whose
vectors come from a hash asserts that *something* was returned; it cannot say that the nearest
passage was the one returned, because "nearest" has no meaning when every vector is noise. So
each passage below carries a `_unit` vector whose cosine similarity to the question is chosen in
advance — `1.0`, `1/√2 ≈ 0.707`, `1/√5 ≈ 0.447`, `0` — and the assertions are arithmetic:
`0.447` clears `RETRIEVAL_MIN_SIMILARITY`'s `0.3` and `0` does not, which is what makes
"below the threshold is not returned" a statement about the threshold.

**The rows are written straight to the tables rather than ingested.** Ingestion decides a
chunk's text, so its vector would have to be scripted against a string the chunker chose; and
this file is not about the pipeline, which `tests/integration/test_knowledge_ingestion.py`
proves separately — including that it writes exactly the rows seeded here.

**Deleting a document is checked in two halves, and neither test is `async`.** The only way to
check the stored object is gone is `app/core/storage.py`'s own reader — a real MinIO call, not a
mock — and that reader is a coroutine. But `knowledge_tasks.ingest_document` runs its own event
loop through `app/core/event_loop.run`, which refuses to start inside a loop that is already
running; an `async def` test *is* such a loop, so awaiting the reader from one is incompatible with
calling the task from the same test. Both halves therefore stay synchronous and reach the async
reader the way every other synchronous caller in this project does — through
`app/core/event_loop.run` — which is also what the Celery tasks themselves use.
"""

import uuid
from collections.abc import Callable, Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai.fake import FakeEmbeddingProvider, FakeProvider
from app.core import event_loop
from app.core.exceptions import StorageUnavailableError
from app.core.storage import read_object
from app.models.knowledge_chunk import EMBEDDING_DIMENSIONS
from app.schemas.knowledge import KnowledgeAnswer
from app.services import knowledge_service
from app.services.knowledge_service import NO_ANSWER
from app.workers import knowledge_tasks
from tests.conftest import KNOWLEDGE, OrgSession

pytestmark = pytest.mark.integration

SEARCH = f"{KNOWLEDGE}/search"
UPLOAD = f"{KNOWLEDGE}/upload"

#: A real markdown policy, so the upload route's signature check sees bytes it admits and the
#: extractor has something to read. Nothing past the leading `#` is inspected by any assertion
#: beyond "a chunk was written".
MARKDOWN = b"# Refunds\n\nRefunds take five working days.\n"


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the sibling integration files do."""


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Retrieval Co")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit(*weights: tuple[int, float]) -> list[float]:
    """A vector that is `weight` on each named axis and zero everywhere else.

    Cosine similarity ignores magnitude, so the numbers here are directions: `_unit((0, 1.0))`
    is the question's own direction and scores `1.0` against it, `_unit((0, 1.0), (1, 1.0))`
    scores `1/√2`, and `_unit((1, 1.0))` — orthogonal — scores `0`. Those three numbers are
    what the threshold test is made of, and they are written down rather than left to a hash.
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


def _document(
    engine: Engine,
    organization_id: str,
    *,
    title: str,
    chunks: Sequence[tuple[str, Sequence[float]]],
    status: str = "completed",
    published: bool = True,
) -> str:
    """Write one document and its chunks, with each chunk's vector chosen by the caller.

    `status` and `published` are parameters because two tests below are about exactly those
    columns: an eligible document's chunks are visible to retrieval and an ineligible one's are
    not, and the pair of flags has two independent ways to make a document invisible.

    Returns the document's id.
    """
    document_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_documents "
                "(id, organization_id, title, content, source_type, status, is_published, "
                " chunk_count) "
                "VALUES (CAST(:id AS uuid), CAST(:org AS uuid), :title, :content, 'manual', "
                " CAST(:status AS processing_status), CAST(:published AS boolean), :count)"
            ),
            {
                "id": document_id,
                "org": organization_id,
                "title": title,
                "content": "\n\n".join(content for content, _ in chunks),
                "status": status,
                "published": published,
                "count": len(chunks),
            },
        )
        for index, (content, vector) in enumerate(chunks):
            conn.execute(
                text(
                    "INSERT INTO knowledge_chunks "
                    "(id, organization_id, document_id, chunk_index, content, embedding, "
                    " embedding_model, token_count) "
                    "VALUES (CAST(:id AS uuid), CAST(:org AS uuid), CAST(:doc AS uuid), "
                    " :index, :content, CAST(:embedding AS vector), :model, 5)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "org": organization_id,
                    "doc": document_id,
                    "index": index,
                    "content": content,
                    "embedding": _literal(vector),
                    "model": "text-embedding-3-small",
                },
            )
    return document_id


def ask(org: OrgSession, question: str) -> dict[str, object]:
    """POST §23's question and return the body, asserting the call succeeded."""
    response = org.post(SEARCH, json={"question": question})
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def answer_with(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> FakeProvider:
    """Point the generation provider at a scripted answer. This is the `_provider` seam."""
    provider = FakeProvider(*outcomes)
    monkeypatch.setattr(knowledge_service.ai_service, "_provider", lambda: provider)
    return provider


def embed_question(
    monkeypatch: pytest.MonkeyPatch, question: str, vector: Sequence[float]
) -> FakeEmbeddingProvider:
    """Script the question's vector, and nothing else — a mismatch is an `AssertionError`.

    `FakeEmbeddingProvider`'s scripted mode raises for a text it was not given, which is what
    keeps this test honest: a question that reached the provider un-scripted would mean the
    string under test is not the string retrieval embedded.
    """
    provider = FakeEmbeddingProvider({question: vector})
    monkeypatch.setattr(knowledge_service.ai_service, "_embedding_provider", lambda: provider)
    return provider


def storage_key(engine: Engine, document_id: str) -> str:
    """A document's object-storage key, read from the row rather than from a response.

    The read model deliberately withholds `source_reference` — it is an internal name for an
    object in a private bucket — so a test that wants to check the object is where the row says
    it is has to read the row.
    """
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT source_reference FROM knowledge_documents WHERE id = CAST(:id AS uuid)"
                ),
                {"id": document_id},
            ).scalar_one()
        )


def chunk_count(engine: Engine, document_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM knowledge_chunks WHERE document_id = CAST(:id AS uuid)"),
                {"id": document_id},
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# Which passages come back
# ---------------------------------------------------------------------------


def test_the_nearest_passages_come_back_in_order_and_the_far_ones_do_not(
    org: OrgSession, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§23's *"retrieve relevant chunks"*, with "relevant" decided by a number.

    Five chunks in one document, four of them with a similarity to the question that is known in
    advance: `1.0`, `1/√2 ≈ 0.707`, `1/√5 ≈ 0.447` — all three above `RETRIEVAL_MIN_SIMILARITY`
    — and two that are not (`0` and `≈ 0.05`). The chunk indexes are deliberately **not** in
    similarity order, so a query that returned the rows in insertion order would fail; and the
    two far chunks are the assertion that the threshold is in the `WHERE` clause rather than
    applied to a top-`k` afterwards, because a `limit`-then-filter would have returned a shorter
    list rather than a correctly-ordered one.

    The citations are the retrieved passages' own text, which is what makes a source checkable
    rather than decorative: a summary of the passage would assert nothing here.
    """
    engine = sync_engine
    question = "How long do refunds take?"
    _document(
        engine,
        _organization(engine, org),
        title="Refund policy",
        chunks=[
            ("Far: an unrelated paragraph.", _unit((1, 1.0))),  # similarity 0.0
            ("Nearest: refunds take five working days.", _unit((0, 1.0))),  # 1.0
            ("Farthest: a weak match.", _unit((0, 1.0), (1, 20.0))),  # ≈ 0.05
            ("Middle: five working days.", _unit((0, 2.0), (1, 3.0))),  # 2/√13 ≈ 0.555
            ("Second: processed within a week.", _unit((0, 1.0), (1, 1.0))),  # ≈ 0.707
        ],
    )
    embed_question(monkeypatch, question, _unit((0, 1.0)))
    answer_with(monkeypatch, KnowledgeAnswer(answer="Five working days.", used_sources=[1, 2, 3]))

    body = ask(org, question)

    similarities = [round(source["similarity"], 3) for source in body["sources"]]
    assert similarities == sorted(similarities, reverse=True)
    assert similarities == [1.0, 0.707, 0.555]
    excerpts = [source["excerpt"] for source in body["sources"]]
    assert excerpts[0] == "Nearest: refunds take five working days."
    assert not any("Far:" in excerpt or "Farthest:" in excerpt for excerpt in excerpts)
    assert body["answer"] == "Five working days."


def test_a_passage_the_model_named_out_of_range_is_not_cited(
    org: OrgSession, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§24's *"do not fabricate citations"*, at the seam where it is enforced.

    The model is handed one passage and names `[1]` and `[7]`. The first is a real passage it
    was given; the seventh is a number for something that does not exist, and the answer comes
    back with **one** source rather than two or none — dropping is not fabricating, and the
    answer itself is still worth returning.
    """
    engine = sync_engine
    question = "How long do refunds take?"
    _document(
        engine,
        _organization(engine, org),
        title="Refund policy",
        chunks=[("Refunds take five working days.", _unit((0, 1.0)))],
    )
    embed_question(monkeypatch, question, _unit((0, 1.0)))
    answer_with(monkeypatch, KnowledgeAnswer(answer="Five days.", used_sources=[1, 7]))

    body = ask(org, question)

    assert len(body["sources"]) == 1
    assert body["sources"][0]["chunk_index"] == 0


# ---------------------------------------------------------------------------
# Which documents are eligible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "published"),
    [
        ("completed", False),  # finished, withheld — the editorial flag
        ("processing", True),  # mid-ingestion, half its chunks at most
        ("failed", True),  # nothing usable was extracted
    ],
)
def test_an_ineligible_document_is_invisible_even_as_the_nearest_match(
    org: OrgSession,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    published: bool,
) -> None:
    """The eligibility rule, one failing half at a time.

    `is_published AND status = completed` is the exact predicate of the partial index the model
    declares for retrieval, and this test is what makes it a rule rather than a coincidence: the
    ineligible document holds a passage that is a **perfect** match for the question, and a
    query that read either column alone would return it. A `failed` document is included because
    the status half is easy to satisfy accidentally — `is_published` is false on a failure, so
    the row is excluded twice over — and a rule that only ever fails one way is one this suite
    would not notice being broken.

    The eligible document beside it is what keeps this from being an assertion that retrieval
    returns nothing: it is a weaker match and it is still returned.
    """
    engine = sync_engine
    question = "How long do refunds take?"
    organization = _organization(engine, org)
    _document(
        engine,
        organization,
        title="Draft: not ready",
        chunks=[("Withheld: refunds take ninety days.", _unit((0, 1.0)))],
        status=status,
        published=published,
    )
    _document(
        engine,
        organization,
        title="Refund policy",
        chunks=[("Refunds take five working days.", _unit((0, 1.0), (1, 1.0)))],
    )
    embed_question(monkeypatch, question, _unit((0, 1.0)))
    answer_with(monkeypatch, KnowledgeAnswer(answer="Five days.", used_sources=[1]))

    body = ask(org, question)

    titles = [source["document_title"] for source in body["sources"]]
    assert titles == ["Refund policy"]
    assert "ninety days" not in str(body)


def test_nothing_above_the_threshold_is_refused_without_calling_a_model(
    org: OrgSession, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§24's last line, and the branch that spends nothing to obey it.

    Real retrieval, real vectors, nothing within `RETRIEVAL_MIN_SIMILARITY`: the answer is this
    server's own sentence with an empty source list. The generation provider is scripted with
    **no outcomes at all**, so a call would be an `AssertionError` rather than a quietly wrong
    answer — which is the only way to state "no model was asked" as a fact about the response.

    The embedding still ran — the tenant has a published chunk, so there is something to search
    — and that distinction is the point: the guard that saves the call is `has_published_chunks`,
    not this branch.
    """
    engine = sync_engine
    question = "How long do refunds take?"
    _document(
        engine,
        _organization(engine, org),
        title="Shipping policy",
        chunks=[("Orders ship in two days.", _unit((1, 1.0)))],  # similarity 0.0
    )
    embeddings = embed_question(monkeypatch, question, _unit((0, 1.0)))
    answer_with(monkeypatch)  # no outcomes: any call is a failure

    body = ask(org, question)

    assert body == {"answer": NO_ANSWER, "sources": []}
    assert embeddings.calls == 1


# ---------------------------------------------------------------------------
# The answer is grounded in what was retrieved
# ---------------------------------------------------------------------------


def test_the_model_is_given_the_retrieved_passages_and_the_question(
    org: OrgSession, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§24's grounding, as a fact about the prompt rather than about the answer.

    The claim that an answer is grounded is only checkable if the passages reached the model, so
    the assertion is on what the provider was handed: both retrieved passages' own text, and the
    question. The far passage's text must be absent — otherwise the prompt carries the whole
    document and the threshold did nothing — which is the half a test that only checked for
    presence would miss.
    """
    engine = sync_engine
    question = "How long do refunds take?"
    _document(
        engine,
        _organization(engine, org),
        title="Refund policy",
        chunks=[
            ("Refunds take five working days.", _unit((0, 1.0))),
            ("Refunds cover shipping too.", _unit((0, 1.0), (1, 1.0))),
            ("Unrelated: our offices are in Lisbon.", _unit((2, 1.0))),
        ],
    )
    embed_question(monkeypatch, question, _unit((0, 1.0)))
    provider = answer_with(
        monkeypatch, KnowledgeAnswer(answer="Five working days.", used_sources=[1])
    )

    body = ask(org, question)

    assert len(provider.requests) == 1
    content = provider.requests[0].content
    assert "Refunds take five working days." in content
    assert "Refunds cover shipping too." in content
    assert "Lisbon" not in content
    assert question in content
    assert body["answer"] == "Five working days."


# ---------------------------------------------------------------------------
# Deleting a document
# ---------------------------------------------------------------------------


def test_deleting_a_document_removes_its_chunks(
    org: OrgSession, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§22's delete, first half: the row goes and the chunks go with it.

    An upload is the case worth testing, because it is the only source that *has* an object,
    and the row here is created through the real route so the ingestion path that writes the
    chunks is the one that produced them.

    The chunks go with the document through the foreign key's `ON DELETE CASCADE` rather than
    through the ORM's cascade — one statement instead of one per chunk — so this is the
    database's cascade being asserted, which is why the count is read straight from the table.

    Ingestion runs here with the hash-derived provider: the pipeline is
    `tests/integration/test_knowledge_ingestion.py`'s subject, and all this test needs of it is
    that the document has chunks to lose.
    """
    created = org.post(
        UPLOAD,
        files={"file": ("policy.md", MARKDOWN, "text/markdown")},
        data={"title": "Refund policy"},
    )
    assert created.status_code == 201, created.text
    document_id = str(created.json()["id"])

    monkeypatch.setattr(knowledge_service.ai_service, "_embedding_provider", FakeEmbeddingProvider)
    ingested = knowledge_tasks.ingest_document(document_id, _organization(sync_engine, org))
    assert ingested["status"] == "completed"
    assert chunk_count(sync_engine, document_id) >= 1

    deleted = org.delete(f"{KNOWLEDGE}/{document_id}")
    assert deleted.status_code == 204, deleted.text

    assert chunk_count(sync_engine, document_id) == 0


def test_deleting_a_document_removes_its_stored_object(
    org: OrgSession, sync_engine: Engine
) -> None:
    """§22's delete, second half: the private object is really gone from storage.

    The upload route stores the file before the row commits, so the object exists from the
    moment the request returns; the read before the delete is what stops this being a test of a
    key that never existed, since a read of a missing key raises the same error as a read of a
    deleted one.

    The object is read through `event_loop.run`, which builds the loop this reader needs, rather
    than through `await`: this test also calls nothing async, and a synchronous test reaching
    storage this way is exactly how `knowledge_tasks` reaches it. `read_object` is the worker's
    reader — the one that fetches the bytes whole — so checking the upload with it is checking
    the same seam ingestion uses.
    """
    created = org.post(
        UPLOAD,
        files={"file": ("policy.md", MARKDOWN, "text/markdown")},
        data={"title": "Refund policy"},
    )
    assert created.status_code == 201, created.text
    document_id = str(created.json()["id"])
    key = storage_key(sync_engine, document_id)

    assert event_loop.run(read_object(key)).startswith(b"# Refunds")

    deleted = org.delete(f"{KNOWLEDGE}/{document_id}")
    assert deleted.status_code == 204, deleted.text

    with pytest.raises(StorageUnavailableError):
        event_loop.run(read_object(key))
