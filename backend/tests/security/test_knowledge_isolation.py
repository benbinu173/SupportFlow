"""§23's *"critical requirement"*: one organization's passages are never another's.

The spec calls tenant isolation of retrieval critical, and it is the one place in this
application where the isolation is *not* structural in the same way as everywhere else. Every
other tenant read goes through `TenantScopedRepository._select`, which adds the
`organization_id` predicate to whatever the caller built; a caller cannot forget it because it
never writes it. The vector search is different: `knowledge_repository.search_chunks` is a
module-level function written for the worker, it takes an `organization_id` as an argument, and
the predicate is *the function's own text*. A missing clause there is a cross-tenant read that
no API test would catch, because the API is not where the query is assembled.

**So the property is asserted on the result, and the vectors are chosen to make it a real
test.** A test that seeded two tenants and found one tenant's rows would pass against a query
that happened to filter on `document_id` instead of the tenant, because each tenant has its own
documents. What makes this file's first test a statement about the *tenant arm* is that the
other organization's passage is the **nearest neighbour by construction**: the question is
scripted to embed onto Southwind's vector exactly, so a search with the tenant clause removed
returns Southwind's chunk first and the assertion fails. That is the deliberate break in this
phase's verification steps, and this is the test it breaks.

**The vectors are hand-written directions rather than hashes**, for the reason
`app/ai/fake.py`'s `FakeEmbeddingProvider` gives about its two modes: a hashed vector is
semantically meaningless, so similarity between a question and a passage would be an accident
rather than a decision. `_unit` builds a one-or-two-hot vector whose similarity to another can
be worked out on paper — `1` for the same direction, `1/√2` for one shared axis — and the
threshold these have to clear is `RETRIEVAL_MIN_SIMILARITY`'s default `0.3`, so `1/√2` is a
comfortable pass rather than a borderline one.

**The two async tests drive `run_ingestion` directly**, as `tests/integration/
test_ai_isolation.py` drives `analyze_ticket`: the worker has no request to read a tenant off,
so both of its tenant predicates — `load_document`'s and `_stage_usage`'s — are reachable only
by calling it, and the assertion that nothing happened is *no embedding call* rather than a
count of completed rows, which a run against the wrong tenant would also produce.
"""

import uuid
from collections.abc import Callable, Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fake import FakeEmbeddingProvider, FakeProvider
from app.core.tenancy import WorkerContext
from app.models.knowledge_chunk import EMBEDDING_DIMENSIONS
from app.schemas.knowledge import KnowledgeAnswer
from app.services import ai_service, knowledge_service
from app.services.knowledge_service import NO_ANSWER
from tests.conftest import KNOWLEDGE, OrgSession

pytestmark = pytest.mark.security

SEARCH = f"{KNOWLEDGE}/search"

#: The model name the ingestion worker stamps on each chunk, repeated here because the rows
#: below are written by hand and have to look like the ones it writes.
EMBEDDING_MODEL = "text-embedding-3-small"


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the other isolation files do."""
    # Nothing to do in the body: `truncate_tables` yields, so depending on it is what places
    # the truncation on the far side of the test.


@pytest.fixture
def northwind(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Northwind Knowledge")


@pytest.fixture
def southwind(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Southwind Knowledge")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit(*hot: int) -> list[float]:
    """A `EMBEDDING_DIMENSIONS`-wide vector, `1.0` at each index in `hot` and `0.0` elsewhere.

    Cosine similarity reads direction and nothing else, so a vector written this way has a
    similarity to another that a reader can compute without running anything: two of these
    sharing no index are orthogonal (`0`), sharing every index are identical (`1`), and sharing
    exactly one are `1/√2 ≈ 0.707`. That last number is what the tests below lean on — it clears
    `RETRIEVAL_MIN_SIMILARITY`'s `0.3` by a wide margin while still being *further* than a
    perfect match, which is what lets one tenant's passage be nearer than the other's.
    """
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for index in hot:
        vector[index] = 1.0
    return vector


def _literal(vector: Sequence[float]) -> str:
    """A pgvector literal — `[1.0,0.0,…]` — for the one place a vector is written by hand."""
    return "[" + ",".join(repr(value) for value in vector) + "]"


def _organization_of(engine: Engine, session: OrgSession) -> str:
    """The organization id behind an authenticated session, read from the user it logged in as.

    From the `users` table rather than a route because there is no route that answers "which
    organization am I" with an id — and this is a test that has to seed rows the API cannot
    create (a published document with a chosen vector), so it is already past the API.
    """
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT organization_id FROM users WHERE id = CAST(:id AS uuid)"),
                {"id": str(session.user_id)},
            ).scalar_one()
        )


def _publish(
    engine: Engine,
    organization_id: str,
    *,
    title: str,
    content: str,
    embedding: Sequence[float],
) -> str:
    """Write one completed, published document with a single chunk. Returns its id.

    Straight to the tables because this file is about *retrieval*, and the vector has to be
    chosen rather than derived: going through ingestion would mean scripting the embedding of a
    chunk whose text the chunker decides, and the whole point below is that the vector is the
    variable. The rows are exactly what a successful ingestion writes — `completed`,
    `is_published`, one chunk carrying a vector and the model's name — so retrieval sees what it
    sees in production.
    """
    document_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_documents "
                "(id, organization_id, title, content, source_type, status, is_published, "
                " chunk_count) "
                "VALUES (CAST(:id AS uuid), CAST(:org AS uuid), :title, :content, 'manual', "
                " 'completed', true, 1)"
            ),
            {"id": document_id, "org": organization_id, "title": title, "content": content},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_chunks "
                "(id, organization_id, document_id, chunk_index, content, embedding, "
                " embedding_model, token_count) "
                "VALUES (CAST(:id AS uuid), CAST(:org AS uuid), CAST(:doc AS uuid), 0, "
                " :content, CAST(:embedding AS vector), :model, 5)"
            ),
            {
                "id": str(uuid.uuid4()),
                "org": organization_id,
                "doc": document_id,
                "content": content,
                "embedding": _literal(embedding),
                "model": EMBEDDING_MODEL,
            },
        )
    return document_id


async def _organization_id(session: AsyncSession, user_id: str) -> uuid.UUID:
    """`_organization_of`'s async twin, for the tests that hold an `AsyncSession`."""
    return (
        await session.execute(
            text("SELECT organization_id FROM users WHERE id = CAST(:id AS uuid)"),
            {"id": user_id},
        )
    ).scalar_one()


async def _pending(
    session: AsyncSession, organization_id: uuid.UUID, *, title: str, content: str
) -> uuid.UUID:
    """A document in the state a create leaves it: `pending`, unpublished, no chunks."""
    document_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO knowledge_documents "
            "(id, organization_id, title, content, source_type, status, is_published) "
            "VALUES (CAST(:id AS uuid), CAST(:org AS uuid), :title, :content, 'manual', "
            " 'pending', false)"
        ),
        {
            "id": str(document_id),
            "org": str(organization_id),
            "title": title,
            "content": content,
        },
    )
    await session.commit()
    return document_id


async def _document_row(session: AsyncSession, document_id: uuid.UUID) -> tuple[str, bool, int]:
    """One document's `(status, is_published, chunk_count)`, as the table holds it."""
    row = (
        await session.execute(
            text(
                "SELECT CAST(status AS text), is_published, chunk_count FROM knowledge_documents "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(document_id)},
        )
    ).one()
    return str(row[0]), bool(row[1]), int(row[2])


async def _ledger_tenants(session: AsyncSession) -> set[str]:
    """The organizations named on the embedding rows in the ledger.

    Deliberately not scoped to a document: `ai_usage` has no document column, and the two tests
    that read this each leave exactly one tenant that could have spent. What is asserted is
    *which* organization the rows name, and a row attributed to the wrong one is the failure
    mode a count of rows would miss.
    """
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT organization_id FROM ai_usage "
                "WHERE operation = 'embed' AND ticket_id IS NULL"
            )
        )
    ).scalars()
    return {str(value) for value in rows}


# ---------------------------------------------------------------------------
# Retrieval across the boundary — §23's critical requirement
# ---------------------------------------------------------------------------


def test_a_nearest_neighbour_in_another_tenant_is_never_returned(
    northwind: OrgSession,
    southwind: OrgSession,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The test this phase's deliberate break exists to fail.**

    Northwind and Southwind each have a published passage. The question is scripted to embed
    onto Southwind's vector *exactly* and onto Northwind's only partly — `1.0` against Southwind
    and `1/√2` against Northwind — so with the query's tenant filter removed the nearest
    neighbour is Southwind's chunk and it is the first source returned. Northwind's own passage
    still clears the threshold, which is what makes the assertion positive rather than a
    restatement of the empty case: the correct row *is* returned, and it is returned **even
    though it is not the nearest row in the table**.

    **What this test does and does not pin, because the query's filter has two arms.**
    `search_chunks` writes `KnowledgeChunk.organization_id` and `KnowledgeDocument.
    organization_id`, and it joins the two tables on `document_id` — so with consistent data
    each arm excludes the other tenant on its own. Removing *one* of them leaves this test green,
    which was checked by removing each; removing **both** fails it, which is the break this test
    is for. The two arms are written anyway for the reason the repository's own docstring gives:
    the chunk column is what the HNSW index pre-filters on, and a join whose tenant arm can be
    omitted is a join whose tenant arm will one day be omitted. A reader should not take this
    test as proof that the chunk-level predicate is load-bearing on its own, because it is not —
    and that is worth stating rather than leaving to be inferred from a passing run.

    The two assertions that carry the weight are the last two. The document id list says which
    row was cited; `"4711"` is Southwind's own text and it must appear nowhere in the response
    — not in a source, not in an answer, not in a title — because a leak through any of those is
    the same leak, and a test that only counted sources would miss the one that reached the
    model's prose.
    """
    question = "How long do refunds take?"
    north_organization = _organization_of(sync_engine, northwind)
    south_organization = _organization_of(sync_engine, southwind)

    north_document = _publish(
        sync_engine,
        north_organization,
        title="Northwind refund policy",
        content="Northwind refunds take five working days.",
        embedding=_unit(0, 1),
    )
    _publish(
        sync_engine,
        south_organization,
        title="Southwind internal notes",
        content="Southwind's vault code is 4711 and its refunds take thirty days.",
        embedding=_unit(0),
    )

    embeddings = FakeEmbeddingProvider({question: _unit(0)})
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: embeddings)
    monkeypatch.setattr(
        ai_service,
        "_provider",
        lambda: FakeProvider(
            KnowledgeAnswer(answer="Refunds take five working days.", used_sources=[1])
        ),
    )

    response = northwind.post(SEARCH, json={"question": question})

    assert response.status_code == 200, response.text
    body = response.json()
    assert [source["document_id"] for source in body["sources"]] == [north_document]
    assert "4711" not in response.text
    assert "Southwind" not in response.text
    # One embedding, of the question: retrieval ran, so the empty-of-Southwind result is the
    # tenant arm's doing rather than the `has_published_chunks` guard's.
    assert embeddings.calls == 1


def test_a_tenant_with_nothing_published_cannot_reach_anothers(
    northwind: OrgSession,
    southwind: OrgSession,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same boundary, from the other side, with the guard as the thing that holds it.

    Northwind has published nothing. Southwind's passage is scripted to be a perfect match for
    the question, so this is the strongest form of the wrong answer: the passage exists, it is
    the nearest in the table, and the caller is entitled to nothing. Two mechanisms have to hold
    and both are asserted — `has_published_chunks` makes the call unnecessary, and the tenant
    arm would stop it anyway.

    **No embedding call, and that is §53 rather than an optimisation.** `RETRIEVAL_MIN_SIMILARITY`
    is not consulted, no model is asked, and the answer is this server's own sentence with an
    empty source list, which is the branch §24 describes for a knowledge base that has nothing.
    """
    question = "How long do refunds take?"
    _publish(
        sync_engine,
        _organization_of(sync_engine, southwind),
        title="Southwind internal notes",
        content="Southwind's vault code is 4711.",
        embedding=_unit(0),
    )

    embeddings = FakeEmbeddingProvider({question: _unit(0)})
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: embeddings)

    response = northwind.post(SEARCH, json={"question": question})

    assert response.status_code == 200, response.text
    assert response.json() == {"answer": NO_ANSWER, "sources": []}
    assert embeddings.calls == 0
    assert "4711" not in response.text


# ---------------------------------------------------------------------------
# The document routes
# ---------------------------------------------------------------------------


def test_the_refusal_is_identical_to_a_document_that_never_existed(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """Status, body and headers — compared in full, on both verbs.

    The `test_ai_isolation.py` property, on the two routes Phase X adds that take an id. A
    refusal that said "that document belongs to another organization" would leak exactly as much
    as a 403 while passing a status-only assertion, and the delete is the more damaging of the
    two: a route that leaked on the write side would destroy another tenant's documents and
    audit the deletion against the wrong organization.
    """
    created = northwind.post(
        KNOWLEDGE, json={"title": "Northwind refund policy", "content": "Refunds take 5 days."}
    ).json()
    missing = uuid.uuid4()

    for verb in ("get", "delete"):
        crossed = getattr(southwind, verb)(f"{KNOWLEDGE}/{created['id']}")
        never_existed = getattr(southwind, verb)(f"{KNOWLEDGE}/{missing}")

        assert crossed.status_code == never_existed.status_code == 404
        assert crossed.json() == never_existed.json()
        assert crossed.headers.get("WWW-Authenticate") is None


def test_each_tenant_lists_only_its_own_documents(
    northwind: OrgSession, southwind: OrgSession
) -> None:
    """The positive half: both tenants have a document, and neither list carries the other's.

    A list route that refused everything would pass the test above; this is the assertion that
    it serves the caller their own rows. Titles rather than counts, because two documents with
    one title each is a shape a filter on the wrong column could still produce.
    """
    northwind.post(
        KNOWLEDGE, json={"title": "Northwind refund policy", "content": "Refunds: five days."}
    )
    southwind.post(
        KNOWLEDGE, json={"title": "Southwind refund policy", "content": "Refunds: thirty days."}
    )

    north_titles = [row["title"] for row in northwind.get(KNOWLEDGE).json()]
    south_titles = [row["title"] for row in southwind.get(KNOWLEDGE).json()]

    assert north_titles == ["Northwind refund policy"]
    assert south_titles == ["Southwind refund policy"]


# ---------------------------------------------------------------------------
# The worker, which has no request to read a tenant from
# ---------------------------------------------------------------------------


async def test_the_worker_will_not_ingest_another_tenants_document(
    northwind: OrgSession,
    southwind: OrgSession,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task handed Northwind's document under Southwind's identity does nothing at all.

    This is `knowledge_repository.load_document`'s `organization_id` predicate, and it is the
    only thing between a worker and guessing which tenant it acts for: the task is handed an
    organization id over a broker, and a redelivered or misconfigured message could carry the
    wrong one. A missing clause here would embed another tenant's document, write its chunks,
    **and stage ledger rows naming the caller's tenant** — spend attributed to an organization
    that never asked for it, which is the same failure `test_ai_isolation.py` guards on the
    analysis side.

    Nothing happens, asserted three ways: the outcome says the row was not there, the provider
    was never asked, and Northwind's document is still `pending` with no chunks. The provider is
    a hashed `FakeEmbeddingProvider` rather than a scripted one, so a call that should not have
    happened would succeed and write rows rather than fail loudly — which is why the *call count*
    is the assertion and the row is the confirmation.
    """
    embeddings = FakeEmbeddingProvider()
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: embeddings)

    north_organization = await _organization_id(db, northwind.user_id)
    south_organization = await _organization_id(db, southwind.user_id)
    document_id = await _pending(
        db,
        north_organization,
        title="Northwind refund policy",
        content="Northwind refunds take five working days.",
    )

    outcome = await knowledge_service.run_ingestion(
        db, WorkerContext(organization_id=south_organization), document_id=document_id
    )

    assert outcome == {"status": "missing", "chunks": 0}
    assert embeddings.calls == 0
    assert await _document_row(db, document_id) == ("pending", False, 0)
    assert await _ledger_tenants(db) == set()


async def test_a_run_embeds_and_charges_its_own_tenants_document(
    northwind: OrgSession,
    southwind: OrgSession,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive half of the test above: the right tenant's document does get ingested.

    Without this, that test would pass against a `run_ingestion` that never worked. Northwind's
    own document is handed to the worker under Northwind's identity and the whole pipeline runs
    — one embedding call, chunks written, the document published — and every ledger row the call
    wrote names Northwind. Southwind has a document too, left `pending`, so the assertion is not
    the weaker "only one document exists".

    The ledger's tenant is the part worth stating: `/analytics/overview` groups by exactly this
    column, so a row attributed elsewhere is spend one tenant can see and another cannot
    reconcile.
    """
    embeddings = FakeEmbeddingProvider()
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: embeddings)

    north_organization = await _organization_id(db, northwind.user_id)
    south_organization = await _organization_id(db, southwind.user_id)
    north_document = await _pending(
        db,
        north_organization,
        title="Northwind refund policy",
        content="Northwind refunds take five working days.",
    )
    south_document = await _pending(
        db,
        south_organization,
        title="Southwind internal notes",
        content="Southwind's vault code is 4711.",
    )

    outcome = await knowledge_service.run_ingestion(
        db, WorkerContext(organization_id=north_organization), document_id=north_document
    )

    assert outcome["status"] == "completed"
    assert embeddings.calls == 1
    status, published, chunks = await _document_row(db, north_document)
    assert (status, published) == ("completed", True)
    assert chunks == outcome["chunks"]
    assert await _document_row(db, south_document) == ("pending", False, 0)
    assert await _ledger_tenants(db) == {str(north_organization)}
