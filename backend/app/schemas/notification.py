"""Notification request and response schemas.

There is no create schema, and that is the design rather than a gap: a client cannot
write a notification any more than it can write an audit entry. Rows are staged by
`app/services/notification_service.py` from inside the transaction of the action they
describe, so the only body this API accepts is the empty one on the two routes that
change a read state.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import NotificationType


class NotificationRead(BaseModel):
    """One notification as the API presents it.

    `user_id` is absent, though the row has one. It is always the caller — the
    repository cannot return another user's row — so echoing it back would be a field
    that is constant per token and invites a client to think it is a parameter.

    `emailed_at` is absent for a different reason: whether the email has gone out is
    delivery trivia the in-app view has no use for. Its home is the row, where a sweep
    can query it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notification_type: NotificationType

    title: str
    body: str

    # The ticket this concerns, when there is one. A client turns it into a link; the
    # notification carries the reference rather than the URL because where a ticket lives
    # is the client's business, and a server-rendered path would have to change with it.
    ticket_id: uuid.UUID | None

    # `None` means unread. A timestamp rather than a boolean, so "when did they read it"
    # is answerable — the same idiom the row uses.
    read_at: datetime | None

    created_at: datetime


class NotificationUnreadCount(BaseModel):
    """The unread badge's number.

    An object rather than a bare integer so the response has somewhere to grow — a
    `{"unread": 3, "oldest": "..."}` costs nothing later, while changing a JSON scalar
    into an object breaks every client that reads it.
    """

    unread: int


class NotificationMarkAllResult(BaseModel):
    """How many notifications a `read-all` actually changed.

    Reported rather than returning `204`: the count is the answer to "did that do
    anything", which a user pressing a button with an empty badge is entitled to know.
    It is also what makes the endpoint's idempotence visible — pressing it twice returns
    `0` the second time rather than pretending to have done work.
    """

    marked_read: int
