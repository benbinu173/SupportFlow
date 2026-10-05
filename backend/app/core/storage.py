"""Object storage — the only module that knows about S3.

Spec §33 puts FastAPI in the middle of the flow:

    Frontend -> FastAPI -> validated upload -> object storage -> database metadata

That ordering is the design, not an implementation detail. **There are no presigned
URLs here**, and there cannot be: a presigned URL is a bearer credential handed to the
client, and the whole point of §33's last line — *"Do not expose private files directly
without authorization"* — is that authorization is checked on every read. A URL cannot
check anything. So bytes go up through the API and come back down through the API, and
`attachments.storage_key` is never given to a client.

Two consequences this module exists to manage:

* **Keys are server-generated.** `build_key` composes the organization id, the ticket
  id, and a fresh UUID. The client's filename never reaches it, which makes §33's
  "Prevent path traversal" a structural property rather than a sanitizing pass someone
  can forget to call. Traversal needs a path, and no client input is in one.
* **boto3 is synchronous.** Every call here blocks, so every call runs in a worker
  thread via `anyio.to_thread`. Calling `put_object` directly from a route would stall
  the event loop for the duration of the transfer — the same class of mistake
  `app/core/event_loop.py` exists to prevent (ADR-011).

Failures are translated, not propagated. botocore's exceptions carry the bucket name
and object key, which is infrastructure detail a client has no business seeing, so they
become `StorageUnavailableError` and the original is logged.
"""

import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import anyio.to_thread
import structlog
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.core.exceptions import StorageUnavailableError

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from botocore.client import BaseClient

logger = structlog.get_logger(__name__)

# Read from storage in 64 KiB pieces. Small enough that a 25 MiB download never sits in
# memory in one piece, large enough that the per-chunk overhead disappears against the
# transfer itself.
CHUNK_SIZE = 64 * 1024

_client: "BaseClient | None" = None


def _shared_client() -> "BaseClient":
    """The process-wide S3 client, built on first use.

    One client, not one per call: botocore clients own a connection pool and a
    credential cache, and building one per request would re-resolve credentials and
    re-open a connection each time. Lazy rather than module-level so importing this
    module never requires storage to be reachable.
    """
    global _client
    if _client is None:
        import boto3  # imported here so the module loads without it installed

        settings = get_settings()
        _client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        )
    return _client


def reset_client() -> None:
    """Drop the cached client. Called on shutdown, and by tests that repoint storage."""
    global _client
    _client = None


def build_key(organization_id: uuid.UUID, ticket_id: uuid.UUID) -> str:
    """The object key for a new upload.

    Three components, none of them client-supplied. The organization and ticket
    prefixes make the bucket browsable by hand during development and make an orphaned
    object attributable to a tenant; the UUID makes the key unique without needing a
    round trip to check. `uuid4().hex` is 32 hex characters — no hyphens, no
    characters that need escaping in a URL or a filesystem path.
    """
    return f"{organization_id}/{ticket_id}/{uuid.uuid4().hex}"


def build_knowledge_key(
    organization_id: uuid.UUID, document_id: uuid.UUID, *, extension: str
) -> str:
    """The object key for a knowledge document's uploaded source file.

    `build_key` with a third component that names a different parent, and the same three
    properties: no client input, one writer per caller, and a UUID that needs no uniqueness
    check. **The tenant is the first component in both**, which is what makes a bucket a
    browsable map of which tenant owns what — the property an orphaned object is diagnosed by.

    **The extension is carried on the key, and it is the one caller-supplied character in it.**
    An upload's text is extracted in a worker rather than in the request that received it, and
    the document row has no content-type column — so without this the worker would have to
    guess how to read the bytes it just fetched. The value is not the client's claim:
    `app/core/file_validation.py` has already refused the upload unless the extension, the
    declared type, and the leading bytes all agreed, so what arrives here is a suffix the
    bytes were shown to own. `app/services/knowledge_service.py` reads it back through
    `EXTENSIONS`, which is the same table that admitted it.

    A separate function rather than a parameter on `build_key`, because the two are not the
    same shape: an attachment belongs to a ticket and a knowledge document does not, and a
    shared builder taking an optional `ticket_id` would be one function with two meanings. The
    prefix is what tells a lifecycle rule, and a person, which kind an object is.
    """
    return f"{organization_id}/knowledge/{document_id}/{uuid.uuid4().hex}{extension}"


def _is_missing(exc: ClientError) -> bool:
    """Whether an S3 error means "no such object" rather than "storage is broken"."""
    return exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}


async def put_object(key: str, fileobj: Any, *, content_type: str) -> None:
    """Store `fileobj` under `key`.

    `fileobj` is a starlette `UploadFile.file` — a real file object, spooled to disk by
    Starlette once an upload passes about a megabyte. boto3 reads it in the worker
    thread, so nothing is buffered here and the bytes are never in this process's
    memory in full.

    `ContentType` is the **detected** type, never the declared one, because the declared
    one is a client claim and this value is what a browser will later be told the file
    is.
    """
    settings = get_settings()

    def _put() -> None:
        _shared_client().upload_fileobj(
            fileobj,
            settings.S3_BUCKET,
            key,
            ExtraArgs={"ContentType": content_type},
        )

    try:
        await anyio.to_thread.run_sync(_put)
    except (BotoCoreError, ClientError, OSError) as exc:
        # OSError as well as the botocore pair: a refused connection surfaces from the
        # socket layer, and "MinIO is not running" is exactly the case this is for.
        logger.error("storage_put_failed", key=key, error=str(exc))
        raise StorageUnavailableError() from exc


async def read_object(key: str) -> bytes:
    """The whole object, for a caller that is going to process the bytes rather than forward them.

    **`open_stream` is the right shape for a response and the wrong one for a worker.** A
    download wants one 64 KiB piece at a time so a 25 MiB attachment never sits in memory; text
    extraction wants the file whole, because a PDF's cross-reference table is at the end and
    pypdf re-reads the buffer as it walks it. So this allocates the object once, on purpose, and
    the caller that uses it is bounded by `MAX_KNOWLEDGE_DOCUMENT_BYTES` before it gets here.

    The read happens in a worker thread, like every other call in this module: `get_object` and
    `body.read()` are both blocking, and a Celery task's loop must not be. The translation is
    `open_stream`'s, including the distinction between "the object is not there" and "storage is
    broken" — the first is an inconsistency worth an error log, and both are 503s.
    """
    settings = get_settings()

    def _read() -> bytes:
        body = _shared_client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"]
        try:
            data: bytes = body.read()
            return data
        finally:
            body.close()

    try:
        return await anyio.to_thread.run_sync(_read)
    except ClientError as exc:
        if _is_missing(exc):
            logger.error("storage_object_missing", key=key)
        else:
            logger.error("storage_get_failed", key=key, error=str(exc))
        raise StorageUnavailableError() from exc
    except (BotoCoreError, OSError) as exc:
        logger.error("storage_get_failed", key=key, error=str(exc))
        raise StorageUnavailableError() from exc


async def delete_object(key: str) -> None:
    """Remove the object under `key`.

    **Idempotent, and that is S3's semantics rather than a decision made here**: deleting a key
    that does not exist succeeds. It suits the one caller — deleting a knowledge document
    removes its stored source as well as its row — because a retried deletion, or a document
    whose object was never written, should not fail the request that is trying to clean up.

    Called **before** the row is deleted, so the pair cannot come apart in the direction that
    matters: an object whose row is gone is an orphan nothing points at and a lifecycle rule can
    collect, and a row whose object is gone is a document an administrator can see and cannot
    re-ingest. The first is a mess; the second is a bug report.
    """
    settings = get_settings()

    def _delete() -> None:
        _shared_client().delete_object(Bucket=settings.S3_BUCKET, Key=key)

    try:
        await anyio.to_thread.run_sync(_delete)
    except (BotoCoreError, ClientError, OSError) as exc:
        logger.error("storage_delete_failed", key=key, error=str(exc))
        raise StorageUnavailableError() from exc


def open_stream(key: str) -> Iterator[bytes]:
    """Yield the stored object's bytes in chunks.

    A **synchronous** iterator, deliberately. `StreamingResponse` runs a sync iterator
    through `iterate_in_threadpool`, so each `read` happens off the event loop and the
    response never holds more than one chunk. Returning an async generator instead
    would mean either blocking the loop inside it or spawning a thread per chunk.

    The body is closed when iteration ends — including when the client disconnects and
    Starlette abandons the generator, which is what keeps abandoned downloads from
    leaking sockets.
    """
    settings = get_settings()

    try:
        body = _shared_client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"]
    except ClientError as exc:
        if _is_missing(exc):
            # The row exists but the object does not. That is an inconsistency between
            # the database and the bucket rather than a missing attachment, so it is
            # logged as an error and reported as a 503 — the client's request was
            # valid and a retry will not help, but it is our fault and not theirs.
            logger.error("storage_object_missing", key=key)
            raise StorageUnavailableError() from exc
        logger.error("storage_get_failed", key=key, error=str(exc))
        raise StorageUnavailableError() from exc
    except (BotoCoreError, OSError) as exc:
        logger.error("storage_get_failed", key=key, error=str(exc))
        raise StorageUnavailableError() from exc

    def _chunks() -> Iterator[bytes]:
        try:
            while chunk := body.read(CHUNK_SIZE):
                yield chunk
        finally:
            body.close()

    return _chunks()


async def ensure_bucket() -> bool:
    """Create the bucket if it is not already there. Returns whether it is usable.

    **Returns rather than raises.** A bucket that cannot be created at startup means
    attachments will not work; it does not mean the API should refuse to serve tickets,
    which is the overwhelming majority of what it does. This mirrors the rate limiter's
    deliberate fail-open (ADR-014): a dependency being down should degrade the feature
    that needs it and nothing else.

    Idempotent — `HeadBucket` first, so the common case is one cheap request and the
    only `CreateBucket` ever issued is the one that succeeds. Concurrent workers racing
    here is harmless: the loser gets `BucketAlreadyOwnedByYou`, which this treats as
    success.
    """
    settings = get_settings()

    def _ensure() -> None:
        client = _shared_client()
        try:
            client.head_bucket(Bucket=settings.S3_BUCKET)
            return
        except ClientError as exc:
            if not _is_missing(exc):
                raise
        client.create_bucket(Bucket=settings.S3_BUCKET)

    try:
        await anyio.to_thread.run_sync(_ensure)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "BucketAlreadyOwnedByYou",
            "BucketAlreadyExists",
        }:
            return True
        logger.warning(
            "storage_bucket_unavailable",
            bucket=settings.S3_BUCKET,
            error=str(exc),
            detail="attachments will fail until storage is reachable",
        )
        return False
    except (BotoCoreError, OSError) as exc:
        logger.warning(
            "storage_bucket_unavailable",
            bucket=settings.S3_BUCKET,
            error=str(exc),
            detail="attachments will fail until storage is reachable",
        )
        return False

    logger.info("storage_bucket_ready", bucket=settings.S3_BUCKET)
    return True
