"""Attachment request and response schemas.

There is deliberately no `AttachmentCreate`. An upload is `multipart/form-data`, which
is not a JSON body and not something a Pydantic model describes — the route takes an
`UploadFile` and the validation that matters happens in `app/core/file_validation.py`,
on bytes rather than on a parsed field.

The read model carries `storage_key` nowhere. That is the point of the whole design:
the key is an internal name for an object in a private bucket, no client has any use for
it, and a client that never sees it cannot ask for it.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AttachmentRead(BaseModel):
    """A stored file's metadata.

    `content_type` is the type **detected from the file's bytes**, not the one the
    upload declared. The declared value is a client claim; this is what the server
    established, and it is what a download is served with.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID

    # The message this arrived with, when it arrived with one. `None` means the file
    # belongs to the ticket rather than to a particular reply — and, because an
    # attachment on an internal note is only visible to a caller who may read internal
    # notes, `message_id` is also what decides visibility. See
    # `app/services/attachment_service.py`.
    message_id: uuid.UUID | None

    uploaded_by_id: uuid.UUID | None

    # The name the client supplied, sanitized for display. Never used to build a path
    # or an object key — `storage.build_key` composes the key from server-side values
    # only, which is what makes path traversal structurally impossible rather than
    # something this field has to be scrubbed for.
    filename: str

    content_type: str
    size_bytes: int

    created_at: datetime
