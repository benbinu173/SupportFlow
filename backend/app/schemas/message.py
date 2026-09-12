"""Message request and response schemas.

`MessageRead` carries `is_internal` because the thread is one list to a staff reader —
an internal note is rendered inline and marked, not returned separately. To a customer
those rows are simply absent, which is a filter the repository applies rather than a
decision this schema makes.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SenderType


class MessageRead(BaseModel):
    """A message as the API presents one."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    sender_type: SenderType
    sender_user_id: uuid.UUID | None
    body: str
    is_internal: bool
    created_at: datetime


class MessageCreate(BaseModel):
    """Posting to a ticket's thread.

    One schema for both the reply and the note endpoint, because the *content* is
    identical and only the audience differs — and the audience is a property of the
    route, not of the body. Putting `is_internal` in the payload would let a caller
    reach the internal-note audience through the reply endpoint if the capability check
    were ever loosened, and the whole point of splitting the routes is that the two
    audiences have two different capabilities.
    """

    body: str = Field(min_length=1, max_length=20_000)
