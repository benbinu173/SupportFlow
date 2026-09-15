"""Attachments — validated uploads, tenant-scoped reads, and the display rules.

Spec §33 asks for four validations (size, content type, file extension, authorization)
and gives one warning that decides how the first three are done:

    Never trust filename or MIME type alone.

So the type is established from the bytes by `app/core/file_validation.py`, the size is
established by counting the stream rather than by reading `Content-Length`, and the
authorization is the ticket's row scope plus the one extra rule this module adds:

> **An attachment that arrived with an internal note is internal.** A customer holds
> `ATTACHMENT_DOWNLOAD` and reaches their own ticket, so nothing in §3's matrix stops
> them at a file attached to a note they cannot read. The file is reachable exactly when
> the message it arrived with is.

That is the divergence ADR-013 predicted between the ticket scope and the attachment
scope. It turned out to be expressible as a *row* rule rather than a *role* rule —
`docs/requirements.md` §3 lines 67-69 say an agent reaches `assigned` attachments and a
customer reaches `own`, which is precisely the ticket map — so the scope mapping is
unchanged and the rule lives here, resolved through the message. ADR-019 records the
correction.

Nothing in this module touches a filesystem path. `storage.build_key` composes the object
key from the organization id, the ticket id, and a fresh UUID, so the client's filename
never becomes part of a path and traversal is impossible rather than sanitized.
"""

import uuid

import structlog
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    ErrorCode,
    FileTooLargeError,
    NotFoundError,
    UnsupportedFileTypeError,
)
from app.core.file_validation import HEAD_SIZE, UnsupportedUpload, sanitize_filename, validate
from app.core.permissions import Permission
from app.core.storage import build_key, put_object
from app.core.tenancy import RequestOrigin, TenantContext
from app.models.attachment import Attachment
from app.models.enums import TicketEventType
from app.models.message import Message
from app.models.ticket import Ticket
from app.repositories.attachment_repository import AttachmentRepository
from app.services import ticket_service

logger = structlog.get_logger(__name__)

# How much of the file is read at a time when measuring it. Large enough that counting a
# 25 MiB upload is a few hundred reads, small enough that nothing is ever held whole.
CHUNK_SIZE = 64 * 1024


def _may_read_internal(context: TenantContext) -> bool:
    """Whether the caller may see files attached to internal notes.

    One capability governs internal notes everywhere: the thread, the timeline, and now
    the files on them. `MESSAGE_READ_INTERNAL` rather than a new `ATTACHMENT_*`
    permission, because the question is "may this caller read internal notes", not
    "may this caller download files" — the second is already `ATTACHMENT_DOWNLOAD` and
    every role holds it.
    """
    return context.has(Permission.MESSAGE_READ_INTERNAL)


async def _measure(upload: UploadFile) -> int:
    """Count the upload's bytes, refusing once it passes the limit.

    Counted by reading rather than taken from `Content-Length`. A header is a claim: a
    client that understates it would otherwise get an unbounded write, and the one place
    the size genuinely matters is the one place a client has an incentive to lie.

    The check is inside the loop so an oversized upload stops at the limit rather than
    after reading all of it — the failure is cheap even when the body is not.
    """
    limit = get_settings().MAX_ATTACHMENT_BYTES
    total = 0
    while chunk := await upload.read(CHUNK_SIZE):
        total += len(chunk)
        if total > limit:
            raise FileTooLargeError(f"Files must be {limit // (1024 * 1024)} MiB or smaller.")
    return total


async def _resolve_message(
    session: AsyncSession, context: TenantContext, ticket: Ticket, message_id: uuid.UUID
) -> Message:
    """The message an upload names, if it is on this ticket and the caller may read it.

    Scoped to the organization by the query itself and to the ticket by `ticket_id`, so
    a message id from another ticket in the same organization is not found rather than
    attached. The caller resolved the ticket first, so the only way to reach here with a
    ticket out of scope is a bug, and the tenant filter catches that too.

    A caller who may not read internal notes may not attach to one. It is a slightly
    narrower rule than "may post a note" — `MESSAGE_POST_INTERNAL` is the capability for
    writing one — but attaching is only meaningful on a note you can see, and requiring
    the read capability keeps one answer to "who may touch internal notes".
    """
    message = await session.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.ticket_id == ticket.id,
            Message.organization_id == context.organization_id,
        )
    )
    if message is None:
        raise NotFoundError(ErrorCode.NOT_FOUND, "Message not found.")

    if message.is_internal and not _may_read_internal(context):
        # The same 404 as a message that does not exist, matching ADR-009: a refusal
        # that distinguishes "not yours" from "not there" confirms a note the caller was
        # never meant to know about.
        raise NotFoundError(ErrorCode.NOT_FOUND, "Message not found.")

    return message


async def create_attachment(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    *,
    upload: UploadFile,
    message_id: uuid.UUID | None,
    origin: RequestOrigin | None = None,
) -> Attachment:
    """Validate and store an upload, then record it. Returns the metadata row.

    **Object storage first, the database row second.** If the insert fails, an object is
    left in the bucket that nothing points at — invisible, unreachable, and reclaimable.
    The other order would leave a row pointing at an object that is not there, which is
    a download that fails for a user who can see the attachment listed. Between a leak
    nobody can observe and an error everybody can, this is the cheaper one.
    """
    ticket = await ticket_service.require_visible_ticket(session, context, ticket_id)

    message: Message | None = None
    if message_id is not None:
        message = await _resolve_message(session, context, ticket, message_id)

    filename = sanitize_filename(upload.filename or "")

    # The signature check. `HEAD_SIZE` bytes is the most any signature in the table
    # needs; the file is then rewound so the rest of it can be counted and uploaded
    # without being held in memory.
    head = await upload.read(HEAD_SIZE)
    try:
        content_type = validate(
            filename=filename,
            declared_content_type=upload.content_type,
            head=head,
        )
    except UnsupportedUpload as exc:
        # The reason names which of the three signals disagreed, which is a map of what
        # the validator accepts. It goes to the log and the client gets the generic
        # message.
        #
        # The filename is deliberately absent, exactly as the ticket subject is absent
        # from `ticket_created` and the body from `message_posted`: it is text the client
        # wrote, and a filename can carry as much of it as a subject can. What an
        # investigation needs is who tried and why it was refused, and `reason`,
        # `organization_id`, and `actor_id` answer both. The reason describes the *shape*
        # of the refusal — an extension, a declared type — never the name it was found on.
        logger.info(
            "attachment_rejected",
            reason=str(exc),
            declared_type=upload.content_type,
            organization_id=str(context.organization_id),
            actor_id=str(context.user_id),
        )
        raise UnsupportedFileTypeError() from exc

    await upload.seek(0)
    size = await _measure(upload)

    # The column is `size_bytes > 0`, so an empty body would be an integrity error
    # rather than a refusal. A zero-byte file has no signature either, so this is the
    # same answer `validate` gives for a body it cannot read — reached here only for a
    # type that has no signature at all, which is why it is not redundant.
    if size == 0:
        raise UnsupportedFileTypeError("An empty file cannot be accepted.")

    key = build_key(context.organization_id, ticket.id)
    await upload.seek(0)
    await put_object(key, upload.file, content_type=content_type)

    attachment = Attachment(
        organization_id=context.organization_id,
        ticket_id=ticket.id,
        message_id=message.id if message is not None else None,
        uploaded_by_id=context.user_id,
        filename=filename,
        storage_key=key,
        # The detected type, not `upload.content_type` — what the bytes are, not what
        # the client said they were.
        content_type=content_type,
        size_bytes=size,
    )
    AttachmentRepository(session, context).add(attachment)
    await session.flush()

    if message is None or not message.is_internal:
        # No event for an internal-note attachment. The note's own `INTERNAL_NOTE_ADDED`
        # entry already covers it, and an `ATTACHMENT_ADDED` entry would put the
        # client-supplied filename on the customer-visible timeline of a file they
        # cannot download. The timeline is read by the ticket's audience; this file's
        # audience is narrower.
        ticket_service.record_event(
            session,
            context,
            ticket,
            TicketEventType.ATTACHMENT_ADDED,
            extra_data={
                "attachment_id": str(attachment.id),
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size,
            },
        )

    await session.commit()

    logger.info(
        "attachment_created",
        attachment_id=str(attachment.id),
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        actor_id=str(context.user_id),
        # The size and detected type, but not the filename: a filename is
        # customer-supplied text and can carry more than a name. The id is enough to
        # find the file, and `attachment_rejected` above already logs the name when an
        # upload is refused — where the name is the whole point of the entry.
        size_bytes=size,
        content_type=content_type,
        ip_address=origin.ip_address if origin else None,
    )
    return attachment


async def list_attachments(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    *,
    limit: int,
    offset: int = 0,
) -> list[Attachment]:
    """A ticket's attachments, oldest first, as much of them as the caller may see.

    The ticket is resolved first, so the scope check is the same one every other ticket
    route uses and a ticket out of reach is a 404 rather than an empty list.
    """
    await ticket_service.require_visible_ticket(session, context, ticket_id)
    return list(
        await AttachmentRepository(session, context).list_for_ticket(
            ticket_id,
            include_internal=_may_read_internal(context),
            limit=limit,
            offset=offset,
        )
    )


async def get_attachment(
    session: AsyncSession, context: TenantContext, attachment_id: uuid.UUID
) -> Attachment:
    """One attachment the caller may download, or 404.

    The row is fetched first and its ticket resolved second, which is the reverse of the
    list route and deliberate: the download route has only an attachment id, and the
    repository's tenant filter is what turns an id from another organization into
    `None` before any ticket is looked at.

    Three ways to get a 404, all indistinguishable:

    * the attachment is in another organization — the repository's tenant predicate;
    * the attachment arrived with an internal note and the caller may not read those;
    * the attachment's ticket is outside the caller's row scope — an agent asking for a
      colleague's ticket's file.

    The third is the one worth naming: the attachment row is theirs, the file is not,
    and the difference must not be observable.
    """
    attachment = await AttachmentRepository(session, context).get_visible(
        attachment_id, include_internal=_may_read_internal(context)
    )
    if attachment is None:
        raise NotFoundError(ErrorCode.ATTACHMENT_NOT_FOUND)

    # The ticket resolution is the third way to 404, and it is caught rather than
    # propagated because `require_visible_ticket` raises `TICKET_NOT_FOUND`. Letting that
    # through would make the three refusals distinguishable after all: a caller holding
    # an attachment id could tell "this file exists, on a ticket that is not mine" from
    # "no such file" by the error code alone, which is exactly the oracle the docstring
    # above says must not exist. The routes are `/attachments/{id}`, so the resource the
    # caller asked for is an attachment and that is what is reported missing.
    try:
        await ticket_service.require_visible_ticket(session, context, attachment.ticket_id)
    except NotFoundError as exc:
        raise NotFoundError(ErrorCode.ATTACHMENT_NOT_FOUND) from exc

    return attachment
