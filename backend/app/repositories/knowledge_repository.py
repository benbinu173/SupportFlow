"""Knowledge-base persistence — the admin list, the vector search, and the worker's reads.

**Two shapes in one file, and which one a function has says who may call it.** The
`KnowledgeDocumentRepository` class is the request path: it is a `TenantScopedRepository`, so
its tenant predicate is structural and no route can ask it for another organization's rows.
The module-level functions are the worker path, exactly as
`app/repositories/ai_repository.py` sets out — a Celery task has no `TenantContext` and must
not invent one, so it passes an `organization_id` that came from a ticket or a document
rather than from a request body. `_select` would refuse a `WorkerContext`, and that refusal
is the point.

**Three of the module-level functions are reached from a request, and they take an
`organization_id` anyway.** That is `ai_service._stage_usage`'s argument rather than
`latest_by_operation`'s: a knowledge read is not scoped by a *ticket*, so there is no object
whose prior resolution could stand in for the check. What names the tenant is the caller's
`TenantContext`, whose `organization_id` came from a verified token
(`app/core/tenancy.py`), and `app/services/knowledge_service.py` is the only thing that calls
them. The alternative — a repository method that spells the same query — would be a second
implementation of the one query whose correctness is the whole feature.

**The vector search is where the HNSW index either is used or is not.** That index is
`vector_cosine_ops`, so retrieval has to ask with `<=>` or Postgres scans the whole table and
discards most of it; `KnowledgeChunk.embedding.cosine_distance` is the pgvector comparator
that emits it, and it is used once, in `_distance`, so there is one place for the operator and
the operator class to disagree. Similarity is `1 - distance`, because cosine distance is what
`<=>` returns and a caller showing "87% match" is owed the honest number rather than the
inverted one.

**Chunks carry `organization_id` and so do documents, and both predicates are written.** A
chunk's tenant column is what the model's own comment says it is for — *"a vector search
filtered by tenant must not require a join to be safe"* — and the document's is written
beside it because this query does join, and a join whose tenant arm can be omitted is a join
whose tenant arm will one day be omitted.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import DocumentSourceType, ProcessingStatus
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.repositories.base import TenantScopedRepository
from app.repositories.search import like_pattern

#: What a document reaped by `fail_abandoned_documents` carries as its explanation. Written by
#: the sweep and read by an admin in the list view, so it is a sentence about what happened
#: rather than a diagnosis; the cause is on the reaper's own log line.
_ABANDONED = "The worker ingesting this document stopped before it finished."


class KnowledgeDocumentRepository(TenantScopedRepository[KnowledgeDocument]):
    """Knowledge documents within the caller's organization.

    Not row-scoped, and there is nothing to scope it by: §3's matrix gives all four knowledge
    capabilities to admin, manager, and agent with no `own` or `assigned` qualifier, and gives
    the customer role none of them. A knowledge base belongs to the organization the way its
    customers do, so the tenant predicate is the whole rule.
    """

    model = KnowledgeDocument

    async def search(
        self, *, term: str | None, limit: int, offset: int
    ) -> Sequence[KnowledgeDocument]:
        """A page of documents, newest first, optionally filtered by title.

        Substring rather than prefix, and served by `ix_knowledge_documents_title_trgm` — the
        trigram index Phase D declared as *"Title search in the admin list view"* and this
        phase is the view. A B-tree cannot answer `%term%`, so the index would be dead weight
        without this method, and a promise nothing keeps is worse than no promise.

        `term=None` and `term=""` both mean no filter, the reading `CustomerRepository.search`
        takes: a query string that arrives empty is not a search for the empty string.

        **Newest first, with `id` as the tiebreak.** The tiebreak is not decoration — offset
        pagination over a non-unique sort key repeats one row and drops another once a table
        is large enough to change plans, which is the same reason `CustomerRepository.search`
        always ends with `Customer.id`.

        **Documents are not filtered by `status` or `is_published`.** This is the administrator's
        list, and a document that is pending, failed, or unpublished is exactly what an admin
        opens it to find out about; retrieval is where the published-and-completed rule lives.
        """
        criteria: list[ColumnElement[bool]] = []
        if term and term.strip():
            criteria.append(KnowledgeDocument.title.ilike(like_pattern(term), escape="\\"))

        statement = (
            self._select(*criteria)
            .order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id)
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def delete(self, document: KnowledgeDocument) -> None:
        """Remove one document row. **The chunks go with it, and the database does that.**

        A Core `DELETE` rather than `session.delete(document)`, because the ORM's
        `delete-orphan` cascade on `KnowledgeDocument.chunks` is a Python-side cascade: it
        loads every chunk of the document and issues one `DELETE` per row, which is five
        hundred round trips for a document that reached the chunk cap. The foreign key is
        `ON DELETE CASCADE`, so one statement removes the document and every chunk of it, and
        the relationship's cascade stays as the safety net it is for a document deleted
        through the ORM somewhere else.

        The tenant predicate is written again here even though the caller loaded `document`
        through `get`. It costs nothing, and this is a *delete*: the one operation where a
        missing predicate is not a widened read but a destroyed row.
        """
        await self.session.execute(
            delete(KnowledgeDocument).where(
                KnowledgeDocument.id == document.id,
                KnowledgeDocument.organization_id == self.organization_id,
            )
        )


# ---------------------------------------------------------------------------
# The worker path, and the two reads the request path shares
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChunkMatch:
    """One passage the vector search returned, with its document's title already joined.

    A flat record rather than a `(KnowledgeChunk, KnowledgeDocument)` pair, because the
    caller needs four fields off the chunk and exactly one off the document, and a pair would
    hand it a loaded `KnowledgeDocument` — including `content`, which is the whole document's
    text — for the sake of its title.

    `similarity` is cosine similarity in `[0, 1]`, computed in the query as `1 - distance`
    rather than reconstructed by the caller: the caller has no way to know the operator that
    produced the distance, and this module is where that is decided.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    content: str
    similarity: float


def _distance(vector: Sequence[float]) -> Any:
    """`embedding <=> vector` — the cosine distance, from the operator the index is built for.

    Typed `Any` and that is deliberate rather than a shortcut: pgvector annotates the
    comparator as `Operators`, which is SQLAlchemy's arithmetic-mixin class and carries
    neither `label` nor `asc`, so the three places this expression is used — the select list,
    the threshold, and the ordering — each need something the annotation does not admit. One
    documented escape hatch, on the one expression whose operator must match an index, beats
    three casts at the call sites.

    **The bind parameter inherits the column's type**, which is what makes the vector reach
    PostgreSQL as a `vector` rather than as an array: pgvector's `Vector` renders it in the
    format the extension parses, and the comparator's left-hand side is what types it.
    """
    return KnowledgeChunk.embedding.cosine_distance(vector)


async def load_document(
    session: AsyncSession, organization_id: uuid.UUID, document_id: uuid.UUID
) -> KnowledgeDocument | None:
    """One document, if it belongs to this organization.

    `None` rather than an exception, for `ai_repository.load_ticket`'s reason: both a missing
    document and a foreign one mean the same thing to the ingestion task — the row it was
    queued for is not there to fill — and the task's job is to log it and stop rather than to
    raise into a broker where a redelivery would fail identically.
    """
    result = await session.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def claim_pending_document(
    session: AsyncSession, organization_id: uuid.UUID, document_id: uuid.UUID
) -> bool:
    """Move this document to `processing` if it is still `pending`. Says whether this caller won.

    **The same conditional statement `ai_repository.claim_pending_analyses` makes, and for the
    same reason.** `run_ingestion` used to read the status, decide in Python, and commit — a
    read followed by a write, which two deliveries of one message can both pass. Here the
    database decides: `WHERE status = 'pending'` matches nothing for the second statement, and
    a `False` return is what tells the caller it has nothing to do. Without it two workers can
    both walk a document through extraction, chunking, and an embedding call, and the second
    one's chunks are a duplicate set that retrieval will return twice.

    No `FOR UPDATE`, for the reason the analysis claim gives: the lock this takes lives only
    until the caller's immediate commit, whereas a row lock held across the fetch, the
    parse, and the embedding call would block the document for the whole task.

    `synchronize_session=False` because the caller claims before it reloads; see the analysis
    function. A caller that had already loaded the row and then read its status would be
    reading the value this statement replaced.
    """
    result = await session.execute(
        update(KnowledgeDocument)
        .where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.organization_id == organization_id,
            KnowledgeDocument.status == ProcessingStatus.PENDING,
        )
        .values(status=ProcessingStatus.PROCESSING)
        .returning(KnowledgeDocument.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


@dataclass(frozen=True, slots=True)
class AbandonedDocumentClaim:
    """One document a sweep returned to `failed`, reduced to what the log line needs."""

    document_id: uuid.UUID
    organization_id: uuid.UUID
    source_type: DocumentSourceType


async def fail_abandoned_documents(
    session: AsyncSession, *, claimed_before: datetime
) -> Sequence[AbandonedDocumentClaim]:
    """Every document claimed before `claimed_before` and still `processing`, marked failed.

    The document-shaped half of `ai_repository.fail_abandoned_claims`, and the argument for it
    is the same one stated there at length. What differs is what a stranded row costs here: an
    analysis in `processing` blocks that operation from ever being requested again, while a
    document in `processing` is invisible to retrieval — `has_published_chunks` and
    `search_chunks` both require `status = 'completed'` — and there is no route that re-queues
    one, so the document is simply gone from the knowledge base with nothing on any screen
    saying so. `failed` with a reason is what puts it back on a screen an admin reads.

    `is_published` is forced false alongside the status, following `knowledge_service._fail`.
    It cannot be true on a `processing` row — nothing sets it before a successful ingestion —
    and writing it makes the invariant read off the row rather than depend on the order of two
    statements somewhere else.

    `updated_at` is the claim's timestamp; see the analysis function for why that column is
    the claim's clock and not a second one.
    """
    result = await session.execute(
        update(KnowledgeDocument)
        .where(
            KnowledgeDocument.status == ProcessingStatus.PROCESSING,
            KnowledgeDocument.updated_at < claimed_before,
        )
        .values(
            status=ProcessingStatus.FAILED,
            error_message=_ABANDONED,
            chunk_count=0,
            is_published=False,
        )
        .returning(
            KnowledgeDocument.id,
            KnowledgeDocument.organization_id,
            KnowledgeDocument.source_type,
        )
        .execution_options(synchronize_session=False)
    )
    return [
        AbandonedDocumentClaim(
            document_id=row.id,
            organization_id=row.organization_id,
            source_type=row.source_type,
        )
        for row in result.all()
    ]


async def has_published_chunks(session: AsyncSession, organization_id: uuid.UUID) -> bool:
    """Whether retrieval could return anything at all for this organization.

    The question a caller asks *before* embedding a question, and the reason it exists is that
    the answer is usually no: a tenant that has never opened the knowledge base has nothing to
    search, and embedding a query to search an empty index spends the organization's money to
    learn what a single indexed read already said. §53 names repeated AI calls as waste, and a
    call that cannot succeed is the purest form of it.

    **An `EXISTS`-shaped read and not a `COUNT`.** The caller needs existence, and
    `ix_knowledge_chunks_org_document` answers that without touching the heap. `LIMIT 1` is
    the same statement Postgres would rewrite `EXISTS` into, written where it is visible.

    `embedding IS NOT NULL` is part of the question rather than defensive: a chunk row is
    written before its vector is known (the column's own comment), and the search excludes a
    null vector because `NULL <=> x` is null. A predicate this function did not share with
    `search_chunks` would let a tenant with only half-ingested rows pay for an embedding to
    search a set the search would then return nothing from.
    """
    statement = (
        select(KnowledgeChunk.id)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(
            KnowledgeChunk.organization_id == organization_id,
            KnowledgeDocument.organization_id == organization_id,
            KnowledgeChunk.embedding.is_not(None),
            KnowledgeDocument.is_published.is_(True),
            KnowledgeDocument.status == ProcessingStatus.COMPLETED,
        )
        .limit(1)
    )
    return await session.scalar(statement) is not None


async def search_chunks(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    vector: Sequence[float],
    limit: int,
    min_similarity: float,
) -> Sequence[ChunkMatch]:
    """The organization's nearest published passages to `vector`, best first.

    **The threshold is in the `WHERE` clause, not applied afterwards.** Filtering the top `k`
    in Python would answer a different question — "of the k nearest, which clear the bar" —
    and would return fewer rows than asked for whenever the k nearest happen to be the weak
    ones, when the honest answer is that further passages should have been considered. In SQL
    it is one predicate, and the ordering and the limit are computed over the rows that pass
    it.

    **`is_published AND status = completed` is the whole eligibility rule** and it is the
    exact predicate of `ix_knowledge_documents_org_published` — the partial index the model
    declares for *"retrieval reads published documents only"*. A document still processing, or
    one that failed, has chunks at most half written and is invisible here; the partial index
    and this clause are one rule with two spellings in two files, which is why the model's
    comment names retrieval.

    Ordered by distance ascending, which is similarity descending: `<=>` is smallest for the
    nearest neighbour, so the natural sort order is already the one a reader wants and no
    `DESC` is needed. `limit` is `RETRIEVAL_TOP_K`, a deployment's policy rather than a
    client's request.
    """
    distance = _distance(vector)
    statement = (
        select(
            KnowledgeChunk.id,
            KnowledgeChunk.document_id,
            KnowledgeChunk.chunk_index,
            KnowledgeChunk.content,
            KnowledgeDocument.title,
            (1 - distance).label("similarity"),
        )
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(
            KnowledgeChunk.organization_id == organization_id,
            KnowledgeDocument.organization_id == organization_id,
            KnowledgeDocument.is_published.is_(True),
            KnowledgeDocument.status == ProcessingStatus.COMPLETED,
            distance <= 1 - min_similarity,
        )
        .order_by(distance)
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return [
        ChunkMatch(
            chunk_id=row.id,
            document_id=row.document_id,
            document_title=row.title,
            chunk_index=row.chunk_index,
            content=row.content,
            similarity=float(row.similarity),
        )
        for row in rows
    ]
