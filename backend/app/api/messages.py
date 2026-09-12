"""Message endpoints — a ticket's conversation.

Mounted under `/tickets/{ticket_id}` rather than at a `/messages` root, because a
message has no independent existence: it is reachable exactly when its ticket is, and
the path says so. A `/messages/{message_id}` route would need its own access rule
derived from the ticket's, which is a second implementation of the row scope waiting to
disagree with the first (ADR-015).

Reading is one route serving two audiences; posting is two routes with two capabilities.
That asymmetry is deliberate. A filter can vary the result of a read by capability
without changing what the route does, but writing an internal note and writing a public
reply are different actions, and giving them one endpoint would mean the endpoint's
declared capability no longer described the request.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Context, DbSession, require_permission
from app.core.permissions import Permission
from app.schemas.message import MessageCreate, MessageRead
from app.services import message_service

router = APIRouter()


@router.get(
    "/{ticket_id}/messages",
    response_model=list[MessageRead],
    summary="A ticket's conversation",
    dependencies=[Depends(require_permission(Permission.MESSAGE_READ_PUBLIC))],
)
async def list_messages(
    ticket_id: uuid.UUID,
    context: Context,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[MessageRead]:
    """A ticket's thread, oldest first.

    Guarded by `MESSAGE_READ_PUBLIC`, which every role holds. Internal notes appear in
    the result only for a caller who also holds `MESSAGE_READ_INTERNAL`, and for anyone
    else they are absent rather than redacted — a redacted entry would disclose that a
    private conversation happened, which is itself information the customer portal has
    no business rendering.

    The ticket is resolved before any message is read, so a ticket out of reach is a
    **404** regardless of who is asking.
    """
    messages = await message_service.list_messages(
        db, context, ticket_id, limit=limit, offset=offset
    )
    return [MessageRead.model_validate(message) for message in messages]


@router.post(
    "/{ticket_id}/messages",
    response_model=MessageRead,
    status_code=status.HTTP_201_CREATED,
    summary="Post a customer-facing reply",
    dependencies=[Depends(require_permission(Permission.MESSAGE_POST_REPLY))],
)
async def post_reply(
    ticket_id: uuid.UUID, payload: MessageCreate, context: Context, db: DbSession
) -> MessageRead:
    """Reply to the customer.

    The author is the authenticated caller and the audience is the customer; neither is
    a request field. The `sender_type` recorded comes from the caller's role, so a
    customer's reply is a customer message and staff replies are agent messages
    whatever else those roles may do.

    Refused on a closed ticket with `422` — reopen it first, which puts it back in front
    of an agent and records that it happened.
    """
    message = await message_service.post_reply(db, context, ticket_id, payload)
    return MessageRead.model_validate(message)


@router.post(
    "/{ticket_id}/notes",
    response_model=MessageRead,
    status_code=status.HTTP_201_CREATED,
    summary="Post an internal note",
    dependencies=[Depends(require_permission(Permission.MESSAGE_POST_INTERNAL))],
)
async def post_note(
    ticket_id: uuid.UUID, payload: MessageCreate, context: Context, db: DbSession
) -> MessageRead:
    """Add a staff-only note to the ticket.

    A separate route from the reply precisely because the audience differs: the payload
    is identical, so the only thing distinguishing the two is the capability their
    routes declare. Folding them together and putting `is_internal` in the body would
    mean a client could name its own audience, and the capability split would be
    decorative.

    Allowed on a closed ticket, unlike a reply. A note never reaches the customer, so it
    cannot reopen a conversation that has ended.
    """
    message = await message_service.post_note(db, context, ticket_id, payload)
    return MessageRead.model_validate(message)
