"""Knowledge-base endpoints — §22's documents and §23's question.

**Six routes, and the shape of the file is the shape of the feature.** Three of them register a
document (from text, from a URL, from a file), two read and remove one, and one asks a question.
§36 names no knowledge paths, so these follow §6's `/api/v1/...` convention and the capability
names in §3: the collection is `/knowledge`, a document is `/knowledge/{document_id}`, the file
transport is `/knowledge/upload`, and the question is `/knowledge/search`.

**Mounting is flat, unlike `analysis` and `messages`.** A knowledge document has no parent — it
is not reachable through a ticket, and it belongs to the organization rather than to a
conversation. So this router is included with the `/knowledge` prefix and every path inside it is
written relative to that, which is the same choice `/customers` and `/sla` make.

**The work is not here.** A create route writes a row, audits it, commits, and hands a task to a
broker; the file is read, the page fetched, and the vectors computed by
`app/workers/knowledge_tasks.py` in another process. §16's *"the API should not wait
unnecessarily"* is satisfied structurally — nothing in this module imports a provider — and the
`201` is honest about it: the document exists and is `pending`, and the ingestion result is not in
this response. The one exception is `search`, which makes a real provider call in the request
path, because a question is asked by a person who is waiting for its answer.

**Upload is its own route, not a field on the first one.** It is a different transport — a
multipart body the server has to validate and store — which is the distinction
`app/api/attachments.py` already draws, and it is why `POST /knowledge` takes JSON and only JSON.

**Which capability a route takes** is read off §3's matrix rather than invented: `KB_UPLOAD` for
the three writes that add a document, `KB_LIST` for the two reads (the matrix has no "view
document" row, so reading takes the list capability — the same reading `GET /customers/{id}`
takes), `KB_DELETE` for the removal, and `AI_QUERY_KNOWLEDGE` for the question. Two are rate
limited: the creates by `limit_upload` because each one eventually costs an embedding, and the
search by `limit_ai` because it calls a provider on the spot (§45, §53).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.api.deps import Context, DbSession, Origin, require_permission
from app.api.rate_limits import limit_ai, limit_upload
from app.core.permissions import Permission
from app.schemas.knowledge import (
    KnowledgeAnswerRead,
    KnowledgeDocumentCreate,
    KnowledgeDocumentRead,
    KnowledgeQuestion,
)
from app.services import knowledge_service

router = APIRouter()


@router.post(
    "",
    response_model=KnowledgeDocumentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a knowledge document from text or a URL",
    dependencies=[
        Depends(require_permission(Permission.KB_UPLOAD)),
        Depends(limit_upload),
    ],
)
async def create_document(
    payload: KnowledgeDocumentCreate,
    context: Context,
    db: DbSession,
    origin: Origin,
) -> KnowledgeDocumentRead:
    """Register a document. **201, and the row is `pending` when it returns.**

    **One route, two source kinds**, because the body can only describe one of them:
    `KnowledgeDocumentCreate` requires exactly one of `content` and `url`, and the service reads
    which was sent rather than trusting a `source_type` the client could set to something else.
    A URL is **not fetched here** — the row is written with the URL as its reference and the
    worker fetches it under the guard in `app/services/url_fetch.py`, so a request cannot be made
    to wait on a stranger's server. A `content` body is stored as the document's text and
    chunked by the same pipeline.

    **202 is not the status, and the distinction is real.** A `202` means "accepted, result
    later"; this returns the document itself, which exists now and is readable at
    `GET /knowledge/{id}` while ingestion runs. The `pending` status is the honest signal, and it
    is on the row rather than in the code.

    **A broker that is down does not fail this request.** §22's document is durable the moment
    the transaction commits; only its ingestion is late, and `enqueue_ingestion` says so in a
    warning rather than by raising.

    Rate limited per user (§45), which is what keeps a loop over this route from buying an
    unbounded embedding bill.
    """
    if payload.url is not None:
        document = await knowledge_service.create_from_url(db, context, payload, origin=origin)
    else:
        document = await knowledge_service.create_manual(db, context, payload, origin=origin)
    return KnowledgeDocumentRead.model_validate(document)


@router.post(
    "/upload",
    response_model=KnowledgeDocumentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a knowledge document from an uploaded file",
    dependencies=[
        Depends(require_permission(Permission.KB_UPLOAD)),
        Depends(limit_upload),
    ],
)
async def upload_document(
    context: Context,
    db: DbSession,
    origin: Origin,
    file: Annotated[UploadFile, File(description="The document to ingest.")],
    title: Annotated[str, Form(description="The document's name in the knowledge base.")],
) -> KnowledgeDocumentRead:
    """Upload a PDF, an HTML page, or a text file as a knowledge document. **201, `pending`.**

    **Four validations, and the bytes decide.** The extension, the declared content type, and
    the leading bytes must all agree (`app/core/file_validation.py`); the size is counted as the
    body streams rather than read from `Content-Length`; and the detected type must be one this
    pipeline can extract text from — a screenshot is a fine attachment and not a document. The
    fourth is the guard above.

    **The file is stored privately and read back by the worker**, not held in this request: the
    bytes go to object storage under a server-generated key, and `run_ingestion` reads them from
    there. That is §33's *"do not expose private files directly"* taken literally — there is no
    presigned URL and no public path, and the key never reaches a client.

    **The original filename is not stored.** The required `title` is the document's name, and
    the schema has no column for anything else; the extension survives only as part of the
    server-generated key, where it tells the worker how to read the bytes.

    A file the extractor cannot read, an empty file, and an unsupported type are all `422`
    (`UnsupportedFileTypeError`), and a file past `MAX_KNOWLEDGE_DOCUMENT_BYTES` is a `413`.
    """
    document = await knowledge_service.create_upload(
        db, context, title=title, upload=file, origin=origin
    )
    return KnowledgeDocumentRead.model_validate(document)


@router.get(
    "",
    response_model=list[KnowledgeDocumentRead],
    summary="List knowledge documents",
    dependencies=[Depends(require_permission(Permission.KB_LIST))],
)
async def list_documents(
    context: Context,
    db: DbSession,
    q: Annotated[str | None, Query(description="Filter by title.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[KnowledgeDocumentRead]:
    """A page of this organization's documents, newest first.

    **`q` filters the title by substring**, served by the trigram index Phase D declared for
    exactly this view — a `%term%` match a B-tree cannot answer. An empty `q` means no filter,
    the reading `CustomerRepository.search` takes.

    **Documents of every status are listed**, including `failed` and `pending` ones. This is the
    administrator's view, and a document that is stuck or broken is precisely what an admin opens
    it to find; the published-and-completed rule belongs to retrieval, not here. `error_message`
    is on the read model for the same reason.
    """
    documents = await knowledge_service.list_documents(
        db, context, term=q, limit=limit, offset=offset
    )
    return [KnowledgeDocumentRead.model_validate(document) for document in documents]


@router.get(
    "/{document_id}",
    response_model=KnowledgeDocumentRead,
    summary="Read a knowledge document",
    dependencies=[Depends(require_permission(Permission.KB_LIST))],
)
async def get_document(
    document_id: uuid.UUID, context: Context, db: DbSession
) -> KnowledgeDocumentRead:
    """One document's metadata and processing state.

    **`KB_LIST` rather than a capability of its own**, because §3's matrix has no "view
    knowledge document" row — the same reading `GET /customers/{id}` → `CUSTOMER_LIST` takes: one
    capability for the operation rather than one per verb.

    A document in another organization is a `404` with `KNOWLEDGE_DOCUMENT_NOT_FOUND`, identical
    to one that does not exist — confirming that an id exists somewhere else is a fact about
    another tenant's data (ADR-009).
    """
    document = await knowledge_service.get_document(db, context, document_id)
    return KnowledgeDocumentRead.model_validate(document)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a knowledge document",
    dependencies=[Depends(require_permission(Permission.KB_DELETE))],
)
async def delete_document(
    document_id: uuid.UUID, context: Context, db: DbSession, origin: Origin
) -> None:
    """Delete a document, its embedded chunks, and its stored source. **204, and audited.**

    **Three things go, and each by the mechanism that owns it.** The chunks are removed by the
    database — `knowledge_chunks.document_id` is `ON DELETE CASCADE` — in the same statement that
    removes the document. The stored object, if this was an upload, is removed first, so the pair
    cannot come apart in the direction that leaves a visible document whose bytes are gone. The
    document row itself goes last, and `KNOWLEDGE_DOCUMENT_DELETED` is written in the same
    transaction, because §34's list has that action and this is the act.

    **204 and not 200**, because there is nothing left to describe: the representation that would
    have been returned no longer exists. A caller that wants to confirm the deletion reads the
    document and gets a 404, which is the same answer it would get for any id it never knew.

    A document in another organization is a `404`, and deleting one is a `KB_DELETE` — a
    capability that, per §3, no manager or agent holds.
    """
    await knowledge_service.delete_document(db, context, document_id, origin=origin)


@router.post(
    "/search",
    response_model=KnowledgeAnswerRead,
    summary="Ask the knowledge base a question",
    dependencies=[
        Depends(require_permission(Permission.AI_QUERY_KNOWLEDGE)),
        Depends(limit_ai),
    ],
)
async def search_knowledge(
    payload: KnowledgeQuestion, context: Context, db: DbSession
) -> KnowledgeAnswerRead:
    """Answer a question from the organization's documents — §23, and §24's refusal.

    **The answer, or the specification's sentence.** If no retrieved passage clears
    `RETRIEVAL_MIN_SIMILARITY`, the response is §24's *"the knowledge base does not contain
    sufficient information"* with an empty `sources` list, and **no model call is made** — there
    is nothing to ground an answer in, so nothing is asked to write one. That is both cheaper
    and strictly more honest than letting a model answer from its priors, and it is why there is
    no `grounded` flag: an empty `sources` list is the whole of that fact.

    **The answer is prose the model wrote; the citations are not.** `sources` is built by
    resolving the model's `used_sources` indices against the passages that were actually
    retrieved and supplied, so a citation a caller sees always names a stored chunk — a forged or
    stale index is dropped rather than trusted (§24's *"do not fabricate citations"*). The order
    is by descending similarity, so the first source is the best match rather than the first one
    the model happened to name.

    **This is a `POST` that reads nothing.** It is a search, and §23 writes it as a query — but a
    question is free text of up to two thousand characters, and a `GET` would put it in a URL and
    therefore in every access log and `Referer` header on the way. A body keeps a customer's
    question out of the parts of the system that record strings.

    Rate limited per user (§45, `limit_ai`), because this route makes a provider call in the
    request path — the one place in this module where a client's request spends money.
    """
    return await knowledge_service.answer(db, context, payload)
