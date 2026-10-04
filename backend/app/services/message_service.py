"""The conversation on a ticket: replies, internal notes, and history.

Every function resolves the ticket through `ticket_service.require_visible_ticket`
before it touches a message. That is the whole access model for a thread — a message is
reachable exactly when its ticket is — so it is implemented once, at the ticket, rather
than as a second `OWN`/`ASSIGNED` predicate on `messages` that could drift from the
first (ADR-015). `MESSAGE_SCOPE_BY_ROLE` remains the declaration of that intent; it
resolves identically to `TICKET_SCOPE_BY_ROLE` by construction.

Two things hang off posting a message besides the row itself:

* **Visibility.** A reply is public and a note is internal, and which one it is follows
  from the endpoint's capability, never from the request body — `MessageCreate` has no
  `is_internal` field. `SenderType` likewise follows from the author's role, through
  the central mapping in `app/core/permissions.py`.
* **`first_response_at`.** Set the first time a member of staff posts a public reply,
  and never by an internal note, which by definition did not reach the customer.
* **Acceptance.** A reply that names an AI draft — §41's "accept" — stages one
  `AI_RESPONSE_ACCEPTED` audit row in the same transaction. The rest of the reply is
  unchanged, because an accepted draft *is* an ordinary reply that happens to have come
  from somewhere; what the draft adds is a record of what was offered and what went out.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cache
from app.core.exceptions import ErrorCode, NotFoundError, ValidationError
from app.core.permissions import SENDER_TYPE_BY_ROLE, Permission
from app.core.tenancy import RequestOrigin, TenantContext
from app.models.enums import AuditAction, SenderType, TicketEventType, TicketStatus
from app.models.message import Message
from app.models.ticket import Ticket
from app.repositories.message_repository import MessageRepository
from app.schemas.message import MessageCreate
from app.services import audit_service, notification_service, ticket_service
from app.websocket import manager as realtime

logger = structlog.get_logger(__name__)


async def list_messages(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> list[Message]:
    """A ticket's conversation, oldest first.

    Internal notes are included only for a caller holding `MESSAGE_READ_INTERNAL`. That
    is the one place this module reads a capability rather than declaring one, and it is
    the right place: the route could not decide it, because the same endpoint has to
    serve a customer and an agent with different results — and a route that branched on
    its caller would be a route whose declared capability no longer describes what it
    does.

    The capability and the repository's filter are both present and neither is
    redundant. The capability is the decision; the filter defaults to excluding
    internal rows. If the two ever disagreed, the failure would be a note that stays
    hidden, not one that leaks.
    """
    ticket = await ticket_service.require_visible_ticket(session, context, ticket_id)

    return list(
        await MessageRepository(session, context).list_for_ticket(
            ticket.id,
            include_internal=context.has(Permission.MESSAGE_READ_INTERNAL),
            limit=limit,
            offset=offset,
        )
    )


async def post_reply(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    payload: MessageCreate,
    *,
    draft_id: uuid.UUID | None = None,
    origin: RequestOrigin | None = None,
) -> Message:
    """Post a customer-facing reply.

    Refused on a `CLOSED` ticket. A closed ticket is finished, and a reply landing on
    one is a message nobody is watching for — the honest answer is to make the caller
    reopen it, which puts the ticket back in front of an agent and records that it
    happened.

    A public reply from staff is the ticket's first response, if it has not had one.
    Recorded here because this is the moment it becomes true; reconstructing it from the
    thread later would mean the timestamp was never authoritative.

    **`draft_id` is §41's accept, and it changes what is recorded rather than what is sent.**
    Given one, the draft is resolved on this ticket and this tenant, and a single
    `AI_RESPONSE_ACCEPTED` row is staged *in this transaction* — §34's before/after carrying
    the draft's body and the body that went out, and `metadata["edited"]` saying whether the
    two differ. §41's remaining verb, *edit*, is that comparison rather than a field: the
    client sends the text it wants sent, and this function cannot tell an edited body from a
    faithful one except by looking.

    **The draft row is never modified.** Accepting re-authors the text as an agent message
    rather than promoting the draft, which is `Message`'s own docstring — *"an AI draft is
    never customer-visible until an agent sends it, at which point it is re-authored as an
    agent message"* — and it is what keeps `messages` append-only, the property Phase V's
    freshness watermark rests on. The draft stays behind as an internal record of what the
    model offered.

    **A `draft_id` naming nothing on this ticket is a 404, and so is one naming a message that
    is not an `ai_draft`.** The two are deliberately the same answer: distinguishing them would
    confirm that a guessed message id exists (ADR-009), and a caller outside the tenant has no
    business telling a draft from a reply in someone else's thread.

    **Accepting the same draft twice is allowed, deliberately.** The draft is an offer rather
    than a claim on the reply; an agent may send the same words twice, and each acceptance is
    its own audit row. Preventing it would need a column recording that a draft had been used
    — a second place recording what the trail already records.

    `origin` exists only so that an acceptance's audit row can name the request it came from.
    An ordinary reply writes no audit row at all, because §34's list has no "message posted"
    entry, so it is `None` for every caller that is not accepting a draft.
    """
    ticket = await ticket_service.require_visible_ticket(session, context, ticket_id)

    if ticket.status is TicketStatus.CLOSED:
        raise ValidationError("This ticket is closed. Reopen it to reply.")

    # Resolved before the message is staged, so a bad `draft_id` cannot leave a pending
    # message behind for a transaction that is about to be refused.
    draft = None
    if draft_id is not None:
        draft = await MessageRepository(session, context).find_draft(ticket.id, draft_id)
        if draft is None:
            raise NotFoundError(ErrorCode.AI_DRAFT_NOT_FOUND)

    message = _post(session, context, ticket, payload, is_internal=False)

    if draft is not None:
        # Staged into this transaction and not one of its own: the audit service's docstring is
        # explicit that a trail written separately *"can disagree with the data it describes"*,
        # and an accepted draft and the message it produced are exactly the pair that would.
        audit_service.record_for(
            session,
            context,
            AuditAction.AI_RESPONSE_ACCEPTED,
            target_type="ticket",
            target_id=ticket.id,
            before=draft.body,
            after=payload.body,
            metadata={"draft_id": str(draft.id), "edited": draft.body != payload.body},
            origin=origin,
        )

    if message.sender_type is SenderType.AGENT and ticket.first_response_at is None:
        ticket.first_response_at = datetime.now(UTC)

    event = ticket_service.record_event(session, context, ticket, TicketEventType.MESSAGE_ADDED)
    # The message is passed, not inferred. `MESSAGE_ADDED` records that the thread grew,
    # not who grew it, and the distinction §26 draws — "new customer reply" — is exactly
    # `sender_type`. Reading it off the caller's role instead would misfile an AI draft,
    # which has no role and must never be mistaken for the customer writing in (§41).
    notifications = await notification_service.notify_for_event(
        session, context, ticket, event, message=message
    )
    await session.commit()
    notification_service.enqueue_delivery(notifications)
    # `internal` is False because `_post` wrote `is_internal=False` above — this is the
    # customer-facing half of the thread, so row scope alone decides the audience.
    await realtime.publish(ticket, event, notifications)
    # A public reply may have stopped the response timer, which moves the compliance
    # number and the response-time average — so a *public* reply invalidates and an
    # internal note below does not. A note never sets `first_response_at` (see the module
    # docstring), so nothing an aggregate reads has changed.
    await cache.invalidate(context.organization_id)

    logger.info(
        "message_posted",
        message_id=str(message.id),
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        sender_type=message.sender_type.value,
        is_internal=False,
        actor_id=str(context.user_id),
    )
    return message


async def post_note(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    payload: MessageCreate,
) -> Message:
    """Post an internal note.

    Allowed on a closed ticket, unlike a reply: a note is bookkeeping for the desk —
    "refunded in full, see the billing thread" — and does not need the ticket reopened
    to be worth writing down. It is also never visible to the customer, so it cannot
    resurrect a conversation that has ended.

    The database enforces the other half of this rule: a `CheckConstraint` refuses an
    internal row whose `sender_type` is `customer`, so a portal caller reaching this
    function through a bug would still not produce one.
    """
    ticket = await ticket_service.require_visible_ticket(session, context, ticket_id)

    message = _post(session, context, ticket, payload, is_internal=True)

    event = ticket_service.record_event(
        session, context, ticket, TicketEventType.INTERNAL_NOTE_ADDED
    )
    await session.commit()
    # **`internal=True`, and this is the flag that does the work.** Row scope alone would
    # hand this event to the ticket's own customer — they own the ticket, so check 2 of
    # `ticket_event_visible_to` passes — and the note they must never read would arrive on
    # their socket as a `ticket.note_added`. The predicate requires
    # `MESSAGE_READ_INTERNAL` when this is set, which is the same capability the HTTP side
    # applies in the service because a route cannot express "this field, for this audience".
    #
    # No notifications: `notify_for_event` has never had a recipient for a note, so there is
    # no `notification.created` to send alongside it and no `enqueue_delivery` above it.
    await realtime.publish(ticket, event, internal=True)
    # No `cache.invalidate` here, deliberately. `post_reply` above invalidates because a
    # public reply can stop the response timer; a note cannot, and no aggregate reads
    # anything else this writes. Invalidating anyway would be a habit rather than a reason.

    logger.info(
        "message_posted",
        message_id=str(message.id),
        ticket_id=str(ticket.id),
        organization_id=str(context.organization_id),
        sender_type=message.sender_type.value,
        is_internal=True,
        actor_id=str(context.user_id),
    )
    return message


def _post(
    session: AsyncSession,
    context: TenantContext,
    ticket: Ticket,
    payload: MessageCreate,
    *,
    is_internal: bool,
) -> Message:
    """Build and stage the message. Does not commit — the caller owns the transaction."""
    message = Message(
        organization_id=context.organization_id,
        ticket_id=ticket.id,
        # From the author's role, via one central mapping rather than a comparison
        # here — see `app/core/permissions.py`. A role that is somehow absent falls back
        # to CUSTOMER, which is the fail-closed reading: an internal row from a customer
        # is refused by a database constraint, so the worst case is a refused write
        # rather than a mislabelled one.
        sender_type=SENDER_TYPE_BY_ROLE.get(context.role, SenderType.CUSTOMER),
        sender_user_id=context.user_id,
        body=payload.body,
        is_internal=is_internal,
    )
    return MessageRepository(session, context).add(message)
