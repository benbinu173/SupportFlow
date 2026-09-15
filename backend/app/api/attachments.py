"""Attachment endpoints — upload, list, and download.

Mounted in two places, and the split is the same one `messages` makes: listing is
`/tickets/{ticket_id}/attachments` because a list is a property of the ticket, and the
download is `/attachments/{attachment_id}` because a download has only an id. The
download route still resolves the ticket underneath — see
`attachment_service.get_attachment` — so it is scoped exactly as the list is.

Both routes take `ATTACHMENT_DOWNLOAD`; only the upload takes `ATTACHMENT_UPLOAD`. §3's
matrix has no separate row for listing, so it is governed by the capability that lets a
caller read the file rather than gaining a capability of its own. That mirrors
`GET /customers/{id}` → `CUSTOMER_LIST`.

**Every byte is served by this process.** There is no presigned URL and no public bucket
path, because §33's last line — "Do not expose private files directly without
authorization" — cannot be honoured by a URL: a URL grants access to whoever holds it,
and authorization is a decision made per request against an authenticated identity.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.deps import Context, DbSession, Origin, require_permission
from app.api.rate_limits import limit_upload
from app.core.file_validation import content_disposition
from app.core.permissions import Permission
from app.core.storage import open_stream
from app.schemas.attachment import AttachmentRead
from app.services import attachment_service

router = APIRouter()


@router.post(
    "/tickets/{ticket_id}/attachments",
    response_model=AttachmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a file to a ticket",
    dependencies=[
        Depends(require_permission(Permission.ATTACHMENT_UPLOAD)),
        Depends(limit_upload),
    ],
)
async def upload_attachment(
    ticket_id: uuid.UUID,
    context: Context,
    db: DbSession,
    origin: Origin,
    file: Annotated[UploadFile, File(description="The file to attach.")],
    message_id: Annotated[
        uuid.UUID | None,
        Form(description="Attach to this message instead of the ticket."),
    ] = None,
) -> AttachmentRead:
    """Upload a file, after validating it and storing it privately.

    Three of §33's four validations happen here; the fourth is the guard above, and the
    row scope underneath it.

    * **Extension, declared type, and the bytes** must all agree, and the bytes decide.
      A file whose leading bytes are not the signature of what it claims to be is a
      `422`, whatever its name says. See `app/core/file_validation.py`.
    * **Size** is counted as the body streams, not read from `Content-Length`, and past
      the configured limit the answer is `413`.
    * **Authorization** is `ATTACHMENT_UPLOAD` and the ticket's row scope, so an agent
      can only attach to a ticket assigned to them and a customer only to their own.

    §45 also names file upload as an endpoint to rate-limit, which is the second guard
    above — per user, not per address, because the caller is authenticated.

    `message_id` is optional and, when present, must name a message on *this* ticket.
    Attaching to an internal note makes the file internal too, which is why the answer
    is a 404 rather than a 403 for a caller who may not read that note.
    """
    attachment = await attachment_service.create_attachment(
        db,
        context,
        ticket_id,
        upload=file,
        message_id=message_id,
        origin=origin,
    )
    return AttachmentRead.model_validate(attachment)


@router.get(
    "/tickets/{ticket_id}/attachments",
    response_model=list[AttachmentRead],
    summary="A ticket's attachments",
    dependencies=[Depends(require_permission(Permission.ATTACHMENT_DOWNLOAD))],
)
async def list_attachments(
    ticket_id: uuid.UUID,
    context: Context,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AttachmentRead]:
    """A ticket's files, oldest first.

    Files attached to an internal note are absent for a caller who may not read internal
    notes, rather than listed and refused on download. A metadata list naming a file the
    caller cannot open is a hint about a private conversation, which is the same
    reasoning that keeps internal notes out of the timeline rather than redacting them.
    """
    attachments = await attachment_service.list_attachments(
        db, context, ticket_id, limit=limit, offset=offset
    )
    return [AttachmentRead.model_validate(attachment) for attachment in attachments]


@router.get(
    "/attachments/{attachment_id}",
    summary="Download an attachment",
    response_class=StreamingResponse,
    dependencies=[Depends(require_permission(Permission.ATTACHMENT_DOWNLOAD))],
)
async def download_attachment(
    attachment_id: uuid.UUID, context: Context, db: DbSession
) -> StreamingResponse:
    """Stream a stored file to a caller entitled to it.

    Three headers carry the security of this response:

    * `Content-Disposition: attachment` — a browser downloads rather than renders. An
      inline response would ask it to interpret a file a customer supplied, in the same
      origin the refresh cookie is scoped to, which is stored XSS with extra steps.
    * `X-Content-Type-Options: nosniff` — the browser must not override the type with
      its own guess. Sent explicitly because a stored file served with a client-chosen
      `Content-Type` is exactly the case this header exists for.
    * `Content-Type` is the **detected** type, stored at upload time from the file's own
      bytes, never the one the uploader declared.

    The body is a synchronous chunk iterator, which Starlette runs in a worker thread, so
    the event loop is never blocked and no more than one chunk is in memory.
    """
    attachment = await attachment_service.get_attachment(db, context, attachment_id)

    # `storage_key` is read from the row and never appears in a response. The client has
    # no use for it and, not having it, cannot ask storage for the object directly.
    return StreamingResponse(
        open_stream(attachment.storage_key),
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": content_disposition(attachment.filename),
            "Content-Length": str(attachment.size_bytes),
            "X-Content-Type-Options": "nosniff",
        },
    )
