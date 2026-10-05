"""§22's pipeline and §23's question, in one module — the knowledge base's service layer.

**Five things this module is, and each one is here rather than somewhere else for a reason.**

*Registering a document* is three functions because there are three ways a document arrives —
typed, fetched, uploaded — and they differ in what the server has to do before the row exists.
They agree afterwards: every one of them ends by queueing the same ingestion task and returning a
`pending` row, which is §22's *"document becomes searchable"* arriving asynchronously.

*Ingesting* is `run_ingestion`, the worker's whole job, so `app/workers/knowledge_tasks.py` is a
decorator and an event loop — `ai_analysis_service.run_analysis`'s arrangement, and for its
reason: the pipeline is testable against a real database with no broker in the picture.

*Retrieving* is `retrieve`, and §21's drafts call it too. *Answering* is `answer`, which is
retrieval plus one grounded call, or §24's sentence and no call at all.

**Nothing in this module is reachable without a context, and the tenant comes from it.** On the
request path that is a `TenantContext` built from a verified token; in the worker it is a
`WorkerContext` built from a document row's organization. §4's *"never trust organization_id
supplied by the frontend"* is not weakened by having two kinds, because neither kind can be
spelled from a request body — `app/services/ai_service.py` makes the same argument about the
same pair.

**Everything that can fail is contained, and the row says so.** A URL that is a private address,
a PDF that is a scan, a provider that is down: each one ends as `status = failed` with a reason
in `error_message` and **no chunks**, which is §22's status column doing what it exists for. The
reason is this module's own text — `str(exc)` on an exception this codebase raised — and never a
provider's raw payload, `app/ai/errors.py`'s existing rule, which is also the rule for this field
because an admin reads it in a list.

**The one call a client pays for is an embedding, and this module refuses to make one it cannot
use.** `retrieve` asks `has_published_chunks` first, so a tenant with no published documents
answers a question without spending anything — and, because that is the state every existing
test is in, §21's drafts keep making exactly the calls they made before Phase X.

**A document's text is extracted in the worker, never in the request.** For an upload the bytes
come from object storage and the type from the key's extension — a suffix
`app/core/file_validation.py` proved the bytes owned. For a URL the page is fetched again, by the
guard in `app/services/url_fetch.py`, rather than in the request that registered it: a request
that fetches is a request that can be made to wait fifteen seconds for a server that answers
nothing, which is §16's *"the API should not wait unnecessarily"* with a stranger's web server on
the other end of it.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.provider import AIRequest
from app.core.config import get_settings
from app.core.exceptions import (
    AIServiceError,
    ErrorCode,
    FileTooLargeError,
    NotFoundError,
    StorageUnavailableError,
    UnsupportedFileTypeError,
    ValidationError,
)
from app.core.file_validation import (
    EXTENSIONS,
    HEAD_SIZE,
    UnsupportedUpload,
    extension_of,
    sanitize_filename,
    validate,
)
from app.core.storage import (
    CHUNK_SIZE,
    build_knowledge_key,
    delete_object,
    put_object,
    read_object,
)
from app.core.tenancy import RequestOrigin, TenantContext, WorkerContext
from app.models.enums import AuditAction, DocumentSourceType, ProcessingStatus
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.repositories import knowledge_repository
from app.repositories.knowledge_repository import ChunkMatch, KnowledgeDocumentRepository
from app.schemas.knowledge import (
    KnowledgeAnswerRead,
    KnowledgeDocumentCreate,
    KnowledgeQuestion,
    KnowledgeSource,
)
from app.services import ai_service, audit_service, document_text, url_fetch
from app.services.document_text import DocumentTextError
from app.services.url_fetch import UrlFetchError

logger = structlog.get_logger(__name__)

#: How many passages are embedded in one call. The vendor bills the same tokens either way, so
#: this trades request count against payload size, and the trade is bounded on both sides: one
#: call per chunk would be forty timeouts with §17's retry policy applied to each, and one call
#: for a whole document would be a request body large enough that a single failure loses all of
#: it. Sixty-four sits well inside `text-embedding-3-small`'s per-request ceiling even at the
#: chunker's target size, which is what the number is chosen against.
EMBEDDING_BATCH_SIZE = 64

#: What a URL source's reference may be, in characters. `knowledge_documents.source_reference` is
#: `String(1000)`, and a long query string on an otherwise ordinary URL is easy to construct and
#: would otherwise surface as a driver error at insert. `app/services/url_fetch.py` applies the
#: same bound to the URL a redirect chain ends at.
MAX_SOURCE_REFERENCE_CHARS = 1000

#: §24's sentence, written by this server and not by a model. It is what a caller gets when no
#: passage clears the similarity threshold, and it lives here rather than in `app/ai/prompts.py`
#: because no prompt produces it — the spec's own words are *"the knowledge base does not contain
#: sufficient information"*, and a model asked to say that could say something else instead, or
#: answer from its priors while appearing to refuse.
NO_ANSWER = "The knowledge base does not contain sufficient information to answer that question."

#: What the fenced block is called in §23's prompt. The caller's own words, per
#: `app/ai/prompts.py`'s rule — the passages are the organization's text and the question is the
#: user's, and a label built from either would be customer text read as instruction.
_PASSAGES_LABEL = "the knowledge base passages retrieved for this question"

#: The failures an ingestion reports on its own row, and the whole list on purpose. Each is a
#: refusal this codebase raised deliberately, whose message is written for a person to read —
#: which is who reads `knowledge_documents.error_message`. Anything *not* listed is a bug, and a
#: bug belongs in the worker's log with its traceback rather than folded into a row.
_FAILURES = (
    DocumentTextError,
    UrlFetchError,
    UnsupportedUpload,
    StorageUnavailableError,
    AIServiceError,
)


# ---------------------------------------------------------------------------
# The request path — registering a document
# ---------------------------------------------------------------------------


async def create_manual(
    session: AsyncSession,
    context: TenantContext,
    payload: KnowledgeDocumentCreate,
    *,
    origin: RequestOrigin | None = None,
) -> KnowledgeDocument:
    """Register a document whose text the client sent. **Commits, then queues.** §22's `manual`.

    `content` is stored as it arrived and is both the document's readable text and the worker's
    source: the pipeline extracts from it the same way it extracts from a fetched page, so a
    pasted policy is cleaned and chunked by one code path rather than by a special case.

    The guard below cannot fire from the route — `KnowledgeDocumentCreate`'s validator requires
    exactly one of `content` and `url` — and it is written rather than assumed because the
    alternative is a `None` reaching `extract`, which would be a type error dressed as a bad
    document.
    """
    content = payload.content
    if content is None:
        raise ValidationError("A manually authored document needs 'content'.")

    document = KnowledgeDocument(
        organization_id=context.organization_id,
        title=payload.title,
        content=content,
        source_type=DocumentSourceType.MANUAL,
        source_reference=None,
        created_by_id=context.user_id,
    )
    return await _register(session, context, document, origin)


async def create_from_url(
    session: AsyncSession,
    context: TenantContext,
    payload: KnowledgeDocumentCreate,
    *,
    origin: RequestOrigin | None = None,
) -> KnowledgeDocument:
    """Register a document to be fetched from `payload.url`. **Commits, then queues.**

    **Nothing is fetched here.** The row is created `pending` with the URL as its reference and
    an empty body, and `run_ingestion` does the fetch inside the SSRF-guarded
    `app/services/url_fetch.py`. That keeps a request from holding a connection open across up
    to six guarded hops and fifteen seconds of timeout each — §16's *"the API should not wait
    unnecessarily"* — and it gives a bad URL the same ending as an unreadable PDF: a `failed`
    document whose reason says which of the two happened, on the row an admin is looking at.

    The length bound is checked here because the column is `String(1000)` and a URL past it is an
    integrity error at insert rather than a refusal. `HttpUrl` has already settled the scheme, so
    there is no `file://` to refuse here; the guard that matters is the address, and it runs in
    the worker.
    """
    url = payload.url
    if url is None:
        raise ValidationError("A document from a URL needs 'url'.")

    reference = str(url)
    if len(reference) > MAX_SOURCE_REFERENCE_CHARS:
        raise ValidationError(f"The URL must be at most {MAX_SOURCE_REFERENCE_CHARS} characters.")

    document = KnowledgeDocument(
        organization_id=context.organization_id,
        title=payload.title,
        # Empty rather than absent: the column is `NOT NULL`, and "not extracted yet" is exactly
        # what `status = pending` says. The worker fills it in, which is why the model's comment
        # calls this the *retained* text rather than the submitted one.
        content="",
        source_type=DocumentSourceType.URL,
        source_reference=reference,
        created_by_id=context.user_id,
    )
    return await _register(session, context, document, origin)


async def create_upload(
    session: AsyncSession,
    context: TenantContext,
    *,
    title: str,
    upload: UploadFile,
    origin: RequestOrigin | None = None,
) -> KnowledgeDocument:
    """Validate and store an uploaded file, then register and queue it. **Commits after storing.**

    §22's `upload`, and it is a route of its own because a multipart body is a different
    transport rather than a different field — the distinction `app/api/attachments.py` already
    draws. Four checks, in the order `app/services/attachment_service.py` uses and for its
    reasons: the **type** from the bytes through the shared validator (never from the filename
    or the declared header alone), the **size** by counting the stream rather than reading
    `Content-Length`, the **readability** of the detected type by this pipeline, and the
    **authorization** of the route's capability.

    **A type the extractor cannot read is refused here, not in the worker.** The upload table is
    shared with attachments and admits a screenshot; a knowledge document has to be text, and
    refusing at upload means the admin learns immediately instead of finding a failed document
    half a minute later. The same set is checked again in `document_text.extract`, because the
    worker is the last place that can still tell what the bytes are.

    **The object is stored after the row is flushed and before the commit**, so a storage failure
    rolls the row back with it and leaves neither. The key needs the document's id, which is what
    the flush is for — `UUIDPrimaryKeyMixin` generates it server-side, so it exists only once the
    database has seen the insert.

    **The original filename is not persisted.** The schema has no column for it and the required
    `title` is the document's name; the extension survives only as part of the server-generated
    key, where it tells the worker how to read the bytes.
    """
    filename = sanitize_filename(upload.filename or "")

    # The signature check. `HEAD_SIZE` bytes is the most any signature in the table needs; the
    # file is then rewound so the rest of it can be counted and uploaded without being held in
    # memory.
    head = await upload.read(HEAD_SIZE)
    try:
        content_type = validate(
            filename=filename,
            declared_content_type=upload.content_type,
            head=head,
        )
    except UnsupportedUpload as exc:
        logger.info(
            "knowledge_upload_rejected",
            reason=str(exc),
            declared_type=upload.content_type,
            organization_id=str(context.organization_id),
            actor_id=str(context.user_id),
        )
        raise UnsupportedFileTypeError() from exc

    if content_type not in document_text.READABLE_MEDIA_TYPES:
        # A screenshot is a perfectly good ticket attachment and not a knowledge document. The
        # detected type goes to the log, where an operator can see what was actually sent; the
        # client gets the same message an unsupported extension gets, because from its position
        # the two are the same refusal.
        logger.info(
            "knowledge_upload_unreadable_type",
            content_type=content_type,
            organization_id=str(context.organization_id),
            actor_id=str(context.user_id),
        )
        raise UnsupportedFileTypeError(
            "A knowledge document must be a PDF, an HTML page, or a text file."
        )

    await upload.seek(0)
    size = await _measure(upload)
    if size == 0:
        raise UnsupportedFileTypeError("An empty file cannot be accepted.")

    document = KnowledgeDocument(
        organization_id=context.organization_id,
        title=title,
        content="",
        # Filled in below, once the key exists. Written as `None` first so there is no window in
        # which the row claims a source it does not have.
        source_reference=None,
        source_type=DocumentSourceType.UPLOAD,
        created_by_id=context.user_id,
    )
    repository = KnowledgeDocumentRepository(session, context)
    repository.add(document)
    await session.flush()

    key = build_knowledge_key(
        context.organization_id, document.id, extension=extension_of(filename)
    )
    await upload.seek(0)
    await put_object(key, upload.file, content_type=content_type)
    document.source_reference = key

    return await _register(session, context, document, origin)


async def list_documents(
    session: AsyncSession,
    context: TenantContext,
    *,
    term: str | None,
    limit: int,
    offset: int = 0,
) -> list[KnowledgeDocument]:
    """A page of this organization's documents, newest first, optionally filtered by title."""
    return list(
        await KnowledgeDocumentRepository(session, context).search(
            term=term, limit=limit, offset=offset
        )
    )


async def get_document(
    session: AsyncSession, context: TenantContext, document_id: uuid.UUID
) -> KnowledgeDocument:
    """One document, or 404 — the same reading `get_customer` takes of a missing row.

    A document in another organization produces the same `KNOWLEDGE_DOCUMENT_NOT_FOUND` a
    document that does not exist produces, because the repository's tenant predicate matches
    nothing either way. That is ADR-009, and it is why there is no separate "not yours" case.
    """
    document = await KnowledgeDocumentRepository(session, context).get(document_id)
    if document is None:
        raise NotFoundError(ErrorCode.KNOWLEDGE_DOCUMENT_NOT_FOUND)
    return document


async def delete_document(
    session: AsyncSession,
    context: TenantContext,
    document_id: uuid.UUID,
    *,
    origin: RequestOrigin | None = None,
) -> None:
    """Delete a document, its chunks, and its stored source. **Commits.** Audits the deletion.

    **The object goes first, then the rows**, which is `app/core/storage.py`'s stated ordering
    and the direction of the two possible messes: an object whose row is gone is unreachable and
    a lifecycle rule can collect it, while a row whose object is gone is a document an admin can
    see and cannot re-ingest. Storage refuses with a 503 and nothing is deleted, so the deleting
    does not half-happen.

    The chunks need no handling here: `knowledge_chunks.document_id` is `ON DELETE CASCADE`, and
    `KnowledgeDocumentRepository.delete` issues one statement for the reason its docstring gives.

    Only an `upload` document has an object. A `manual` one has no `source_reference` at all and
    a `url` one has the client's own URL, which is not storage's to remove.
    """
    repository = KnowledgeDocumentRepository(session, context)
    document = await repository.get(document_id)
    if document is None:
        raise NotFoundError(ErrorCode.KNOWLEDGE_DOCUMENT_NOT_FOUND)

    if document.source_type is DocumentSourceType.UPLOAD and document.source_reference:
        await delete_object(document.source_reference)

    audit_service.record_for(
        session,
        context,
        AuditAction.KNOWLEDGE_DOCUMENT_DELETED,
        target_type="knowledge_document",
        target_id=document.id,
        metadata={"title": document.title, "source_type": str(document.source_type)},
        origin=origin,
    )
    await repository.delete(document)
    await session.commit()

    logger.info(
        "knowledge_document_deleted",
        document_id=str(document.id),
        organization_id=str(context.organization_id),
        actor_id=str(context.user_id),
    )


async def _register(
    session: AsyncSession,
    context: TenantContext,
    document: KnowledgeDocument,
    origin: RequestOrigin | None,
) -> KnowledgeDocument:
    """Audit, commit, and queue one new document. The three `create_*` tails, written once.

    Shared because the tail is where the three arrivals stop differing: the row is staged (by
    the caller, which for an upload also stored the object and set the key), the audit entry is
    written in the same transaction as the row, the commit makes it visible, and only then is
    the task handed the id — `enqueue_analysis`'s ordering, and its reason: a task that started
    before the row was visible would find nothing and quietly do nothing.

    The audit row is written here rather than in the route for `request_analysis`'s reason: §34
    lists the action, and this is the moment it happens.
    """
    session.add(document)
    audit_service.record_for(
        session,
        context,
        AuditAction.KNOWLEDGE_DOCUMENT_CREATED,
        target_type="knowledge_document",
        target_id=document.id,
        metadata={"title": document.title, "source_type": str(document.source_type)},
        origin=origin,
    )
    await session.commit()

    enqueue_ingestion(document.id, context.organization_id)

    logger.info(
        "knowledge_document_created",
        document_id=str(document.id),
        organization_id=str(context.organization_id),
        source_type=str(document.source_type),
        actor_id=str(context.user_id),
    )
    return document


def enqueue_ingestion(document_id: uuid.UUID, organization_id: uuid.UUID) -> int:
    """Hand one document to the broker. **Call this after the commit.**

    Synchronous, and after the commit for `enqueue_analysis`'s reason: the task's first act is
    to read the row it was handed the id of, and a task that started before that row was visible
    would find nothing and quietly do nothing.

    **A broker that is down does not fail the request.** The row is committed and shows as
    `pending`, so the user's action succeeded; only the work is late.
    `ix_knowledge_documents_pending` is partial on exactly the status pair that makes a stuck row
    findable, which is the index the model declares as *"ingestion queue and retry sweep"*.

    Returns how many were queued — `0` or `1`, `enqueue_delivery`'s shape — so a caller or a test
    can tell "nothing to do" from "queued and forgotten".
    """
    # Imported here rather than at module scope so the API process does not import the task
    # module on every startup, following `enqueue_delivery` and `enqueue_analysis`. Nothing
    # above this line needs Celery to exist, and the dependency stays visible at its one use.
    from app.workers.knowledge_tasks import ingest_document

    try:
        ingest_document.delay(str(document_id), str(organization_id))
    except Exception as exc:
        logger.warning(
            "knowledge_ingestion_not_queued",
            document_id=str(document_id),
            organization_id=str(organization_id),
            error_type=type(exc).__name__,
        )
        return 0
    return 1


# ---------------------------------------------------------------------------
# The worker path — §22's pipeline, end to end
# ---------------------------------------------------------------------------


async def run_ingestion(
    session: AsyncSession, context: WorkerContext, *, document_id: uuid.UUID
) -> dict[str, str | int]:
    """Extract, clean, chunk, embed, and publish one document. **Commits, and commits failures.**

    §22's pipeline in the order the spec draws it, and the whole of the worker's work so the task
    module is a decorator and an event loop.

    **Two commits before the work starts and after it ends, and the first one is a claim.** The
    row is flipped to `processing` before any network call, so "running" is a state another
    process can see — which is what makes a redelivered task a no-op rather than a second
    embedding bill, and the task is declared with `acks_late` so at-least-once delivery is the
    contract.

    **A row that is not `pending` is skipped, and that is stricter than `run_analysis`.** There,
    `pending` and `processing` are both claimable; here one delivery owns the document, because
    the alternative is two workers appending the same chunks and colliding on
    `uq_knowledge_chunks_document_id_chunk_index`. A crash mid-ingestion therefore leaves a
    document in `processing` that nothing will pick up, and the recovery is the delete route and
    a fresh upload — which is honest, visible in the list, and not a silent half-ingestion.

    **The ledger rows from a failure are committed.** `ai_service` never commits its own rows,
    and a failed embedding is still spend; `_fail` below commits them with the row, which is
    `run_analysis`'s "the ledger rows are committed even when every operation failed".

    Returns a status and a count for the result backend — a debugging aid, exactly as
    `analyze_ticket`'s docstring says: the durable record is the row.
    """
    document = await knowledge_repository.load_document(
        session, context.organization_id, document_id
    )
    if document is None:
        # Deleted between the queue and the run. `knowledge_chunks.document_id` cascades, so
        # there is nothing left to fill and nothing to report; this is a normal outcome.
        logger.warning(
            "knowledge_ingestion_document_missing",
            document_id=str(document_id),
            organization_id=str(context.organization_id),
        )
        return {"status": "missing", "chunks": 0}

    if document.status is not ProcessingStatus.PENDING:
        logger.info(
            "knowledge_ingestion_skipped",
            document_id=str(document.id),
            organization_id=str(context.organization_id),
            status=str(document.status),
        )
        return {"status": "skipped", "chunks": 0}

    document.status = ProcessingStatus.PROCESSING
    await session.commit()

    try:
        text = await _source_text(document)
        passages = document_text.chunk(text)
        if not passages:
            # `extract` refuses an empty document, so this is unreachable through the three
            # sources above — and it is here because "no passages" and "chunks were written"
            # must not be the same outcome, which is what an unguarded empty list would make
            # them. A `completed` document with no chunks answers nothing and looks fine.
            raise DocumentTextError("the document produced no passages")
        vectors = await _embed(session, context, passages)
    except _FAILURES as exc:
        await _fail(session, document, reason=str(exc))
        return {"status": "failed", "chunks": 0}

    settings = get_settings()
    for passage, vector in zip(passages, vectors, strict=True):
        session.add(
            KnowledgeChunk(
                organization_id=document.organization_id,
                document_id=document.id,
                chunk_index=passage.index,
                content=passage.content,
                embedding=vector,
                # Stamped per chunk rather than read at query time, which is what makes a stale
                # vector sweep possible after a model change — the column's own comment. This
                # phase writes it and does not read it.
                embedding_model=settings.EMBEDDING_MODEL,
                token_count=passage.token_count,
            )
        )

    # The whole document's text, kept so it can be re-chunked after a strategy change without
    # re-fetching the source. It is also what a manual document already holds, so this line is a
    # no-op for one — written unconditionally rather than branched, so the invariant "a completed
    # document's `content` is what was extracted" has one statement behind it.
    document.content = text
    document.chunk_count = len(passages)
    document.status = ProcessingStatus.COMPLETED
    document.processed_at = datetime.now(UTC)
    document.error_message = None
    # §22's last step, and the one column that decides whether retrieval can see any of this.
    # Set here and not by a route: publishing is not an admin action in this phase, it is what
    # finishing means — see the column's own comment on why the two columns stay separate.
    document.is_published = True
    await session.commit()

    logger.info(
        "knowledge_document_ingested",
        document_id=str(document.id),
        organization_id=str(context.organization_id),
        source_type=str(document.source_type),
        chunks=len(passages),
    )
    return {"status": "completed", "chunks": len(passages)}


async def _source_text(document: KnowledgeDocument) -> str:
    """The document's text, from wherever this kind of document keeps it. Cleaned.

    Three sources, one function, and the branch is on `source_type` rather than on which fields
    happen to be set — the column exists to say where a document came from, and reading it is
    more honest than inferring.

    **A manual document goes through `extract` like the other two.** Its text is already text, so
    this costs one encode and one decode of at most `_MAX_CONTENT_CHARS`; what it buys is that
    cleaning, the non-empty check, and the type refusal are one code path rather than three
    special cases, and a pasted policy with Windows line endings is cleaned the same way a
    fetched page is.

    **The upload branch reads the media type off the key's extension**, through
    `app/core/file_validation.py`'s own table. The suffix was written by this application out of
    a type the bytes were shown to own, so it is a server-side fact rather than a client claim —
    which is the whole reason it was put there.
    """
    reference = document.source_reference

    if document.source_type is DocumentSourceType.MANUAL:
        return document_text.extract(document.content.encode("utf-8"), "text/plain")

    if reference is None:
        raise DocumentTextError("this document has no source to ingest from")

    if document.source_type is DocumentSourceType.URL:
        page = await url_fetch.fetch(reference)
        return document_text.extract(page.body, page.content_type)

    media_type = EXTENSIONS.get(extension_of(reference))
    if media_type is None:
        raise DocumentTextError("the stored file's type could not be established from its key")
    return document_text.extract(await read_object(reference), media_type)


async def _embed(
    session: AsyncSession, context: WorkerContext, passages: Sequence[document_text.TextChunk]
) -> list[list[float]]:
    """Every passage's vector, in the order the passages were given. One call per batch.

    `EMBEDDING_BATCH_SIZE` at a time, because the vendor bills the same tokens either way and
    forty calls would be forty round trips with §17's retry policy applied to each. One ledger row
    is staged per batch by `ai_service.embed_texts`, which is the honest unit: a batch is one
    request to a vendor and one line of spend.

    **The order is the contract.** `knowledge_chunks.chunk_index` is what makes a passage's
    position recoverable, and it comes from the chunker rather than from this list — so a
    mis-ordered vector would attach a passage to the wrong number silently. `ai_service` returns
    the vectors in the order the texts were given and the provider checks that there is one per
    input; the `zip(..., strict=True)` at the call site is what makes a violation a crash rather
    than a misalignment.
    """
    vectors: list[list[float]] = []
    for start in range(0, len(passages), EMBEDDING_BATCH_SIZE):
        batch = passages[start : start + EMBEDDING_BATCH_SIZE]
        embedded = await ai_service.embed_texts(
            session, context, [passage.content for passage in batch]
        )
        vectors.extend(embedded.value.vectors)
    return vectors


async def _fail(session: AsyncSession, document: KnowledgeDocument, *, reason: str) -> None:
    """Write a failed document. **Commits**, with whatever the attempt staged. Never raises.

    `reason` is a sentence this codebase wrote, and it is stored rather than logged alone because
    it is the only thing that tells an admin why a document they added is not answering anything.
    No provider text reaches it: `AIServiceError.message` is the fixed sentence
    `app/core/exceptions.py` gives that class, and the other failures carry this project's own
    wording.

    `is_published` is forced false alongside the status. It cannot be true here — nothing sets it
    before a successful ingestion — and writing it makes the invariant read off the row rather
    than depend on the order of two statements in `run_ingestion`.
    """
    document.status = ProcessingStatus.FAILED
    document.error_message = reason
    document.chunk_count = 0
    document.is_published = False
    await session.commit()

    logger.error(
        "knowledge_ingestion_failed",
        document_id=str(document.id),
        organization_id=str(document.organization_id),
        source_type=str(document.source_type),
        reason=reason,
    )


# ---------------------------------------------------------------------------
# §23 — retrieving passages, and answering from them
# ---------------------------------------------------------------------------


async def retrieve(
    session: AsyncSession,
    context: TenantContext | WorkerContext,
    query: str,
    *,
    ticket_id: uuid.UUID | None = None,
) -> list[ChunkMatch]:
    """The passages nearest `query` in this organization's published knowledge base.

    **One embedding call, and none at all when there is nothing to search.** The guard is the
    first thing this function does, so an organization that has never ingested a document pays
    nothing to ask a question it cannot be answered — and §21's drafts, which call this too, keep
    making exactly the calls they made before Phase X.

    The threshold is `RETRIEVAL_MIN_SIMILARITY` and it is applied by the query, not here: §24's
    *"if no relevant source is found"* is a decision about distance, and a decision made in the
    `WHERE` clause is one a reader can check against the ordering beside it. An empty return is
    therefore "nothing was close enough" rather than "nothing was found", and the caller answers
    the same way for both — §24's refusal is the honest response to either.

    **A failure propagates, and the caller decides what it means.** The search route turns it
    into a 503, because a question the product could not answer is not a question the product
    answered with nothing. §21's draft path catches it and drafts anyway — a knowledge base being
    down must not stop a reply being written, which is a policy of that caller rather than of
    retrieval, which is why it lives there.
    """
    settings = get_settings()
    organization_id = context.organization_id

    if not await knowledge_repository.has_published_chunks(session, organization_id):
        logger.info("knowledge_retrieval_empty", organization_id=str(organization_id))
        return []

    embedded = await ai_service.embed_texts(session, context, [query], ticket_id=ticket_id)
    matches = await knowledge_repository.search_chunks(
        session,
        organization_id,
        vector=embedded.value.vectors[0],
        limit=settings.RETRIEVAL_TOP_K,
        min_similarity=settings.RETRIEVAL_MIN_SIMILARITY,
    )
    logger.info(
        "knowledge_retrieved",
        organization_id=str(organization_id),
        passages=len(matches),
        best_similarity=round(matches[0].similarity, 4) if matches else None,
    )
    return list(matches)


async def answer(
    session: AsyncSession,
    context: TenantContext,
    payload: KnowledgeQuestion,
    *,
    ticket_id: uuid.UUID | None = None,
) -> KnowledgeAnswerRead:
    """§23's question, answered from the knowledge base or refused. **Commits nothing.**

    Two branches, and the difference between them is the whole of §24.

    **Nothing above the threshold: no model call, no ledger row, no answer from priors.** The
    response is `NO_ANSWER` with `sources: []`. It is the cheaper branch and the more honest one —
    there is no model in the loop to hallucinate a policy, and nothing that could fabricate a
    citation — and it is what §24's last line asks for in as many words.

    **Something above it: one grounded call.** `prompts.knowledge_content` numbers the passages
    and `prompts.KNOWLEDGE_INSTRUCTION` tells the model to use only them, to say when they do not
    answer, and to name the ones it used.

    **Citations are resolved against the passages this function supplied, never taken from the
    model's prose.** `used_sources` holds 1-based numbers into that list; an index outside it is
    dropped with a log line rather than raised on, because dropping is not fabricating and a
    model that cited `[7]` when handed three passages still produced a usable answer. That is
    §24's *"do not fabricate citations"* served by a mapping rather than by a hope.

    **No commit here.** `ai_service` stages its ledger rows and the caller owns the transaction;
    the route's session commits them at the end of the request, including on the branch that made
    no call — where there is no row to commit, which is the point of that branch.
    """
    matches = await retrieve(session, context, payload.question, ticket_id=ticket_id)
    if not matches:
        logger.info(
            "knowledge_not_answered",
            organization_id=str(context.organization_id),
            actor_id=str(context.user_id),
        )
        return KnowledgeAnswerRead(answer=NO_ANSWER, sources=[])

    answered = await ai_service.answer_question(
        session,
        context,
        _answer_request(payload.question, matches),
        ticket_id=ticket_id,
    )
    return KnowledgeAnswerRead(
        answer=answered.value.answer,
        sources=_cite(answered.value.used_sources, matches),
    )


def _answer_request(question: str, matches: Sequence[ChunkMatch]) -> AIRequest:
    """The §23 call for one question and the passages retrieved for it.

    `max_tokens` comes from settings, the reading every other request builder in this project
    takes: `AIRequest`'s docstring gives it one home. The fence is the provider's —
    `prompts.knowledge_content` renders the question and the numbered passages and stops there,
    which is what keeps one implementation of the mechanism rather than two.
    """
    return AIRequest(
        instruction=prompts.KNOWLEDGE_INSTRUCTION,
        content=prompts.knowledge_content(question, [match.content for match in matches]),
        content_label=_PASSAGES_LABEL,
        max_tokens=get_settings().AI_MAX_TOKENS,
    )


def _cite(used_sources: Sequence[int], matches: Sequence[ChunkMatch]) -> list[KnowledgeSource]:
    """The citations a caller receives, from the numbers the model returned.

    Two things happen here and both are deliberate.

    **An index that names no supplied passage is dropped, with a log line.** The model is asked
    for numbers into a list it was given, and a number outside that list is either a mistake or a
    forgery; neither is a reason to fail an answer that may be perfectly good, and neither is a
    reason to build a citation to a chunk that was never retrieved. Dropping is not fabricating,
    which is the property §24 asks for. `prompts._citable_passages`'s own comment notes that a
    passage's text can contain a line shaped like a citation number — this is what makes that
    harmless.

    **The result is in retrieval order, not in the order the model named them.** The passages
    arrive sorted by descending similarity, so the first source is the passage retrieval
    considered the best match; a caller reading citations in the order a model happened to type
    them would learn nothing about which was closest. It also de-duplicates: a passage named twice
    is one passage, and a set is the honest way to say that.
    """
    cited = set()
    for number in used_sources:
        if 1 <= number <= len(matches):
            cited.add(number - 1)
        else:
            logger.warning("knowledge_citation_out_of_range", number=number, passages=len(matches))

    return [
        KnowledgeSource(
            document_id=match.document_id,
            document_title=match.document_title,
            chunk_index=match.chunk_index,
            # The passage's own text, not a summary of it: a citation a reader cannot check is
            # decoration. Bounded by the chunker rather than by the schema, which says so.
            excerpt=match.content,
            similarity=match.similarity,
        )
        for position, match in enumerate(matches)
        if position in cited
    ]


async def _measure(upload: UploadFile) -> int:
    """Count the upload's bytes, refusing once it passes the limit.

    `attachment_service._measure`'s function against a different ceiling, and the reasons are
    that one's: the size is counted by reading rather than taken from `Content-Length`, because a
    header is a claim and the one place the size matters is the one place a client has an incentive
    to lie; and the check is inside the loop so an oversized upload stops at the limit rather than
    after reading all of it.

    The ceiling is `MAX_KNOWLEDGE_DOCUMENT_BYTES` rather than the attachment one. A document is
    embedded rather than stored and downloaded, so this is the knob that decides what one
    document can cost — which is why it exists as a setting at all.
    """
    limit = get_settings().MAX_KNOWLEDGE_DOCUMENT_BYTES
    total = 0
    while chunk := await upload.read(CHUNK_SIZE):
        total += len(chunk)
        if total > limit:
            raise FileTooLargeError(f"Documents must be {limit // (1024 * 1024)} MiB or smaller.")
    return total
