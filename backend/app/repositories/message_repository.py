"""Message persistence.

The visibility rule is a filter this repository always applies, never one a caller may
forget to request: `list_for_ticket` excludes internal notes unless the caller
explicitly asks for them, and the only caller that asks is one holding
`MESSAGE_READ_INTERNAL`. A note wrongly shown leaks; a note wrongly hidden merely
hides — so the default is the restrictive one.
"""

import uuid
from collections.abc import Sequence

from app.models.enums import SenderType
from app.models.message import Message
from app.repositories.base import TenantScopedRepository


class MessageRepository(TenantScopedRepository[Message]):
    """Messages within the caller's organization.

    Not row-scoped, deliberately. A message has no visibility rule of its own — it is
    reachable exactly when its ticket is — so the scope is applied by resolving the
    ticket through `TicketRepository` before this repository is ever reached. That is
    one implementation of `OWN`/`ASSIGNED` rather than two that can drift, and
    ADR-015 records it as the reason `MESSAGE_SCOPE_BY_ROLE` is a declaration of intent
    applied at the ticket rather than a second predicate built here.

    The organization filter still applies, independently. A message id from another
    tenant finds nothing even if a bug upstream handed this repository the wrong
    `ticket_id`.
    """

    model = Message

    async def list_for_ticket(
        self, ticket_id: uuid.UUID, *, include_internal: bool, limit: int, offset: int = 0
    ) -> Sequence[Message]:
        """One ticket's thread, oldest first, as much of it as the caller may see.

        Ascending because a conversation is read forwards — the opposite of the ticket
        queue, and the direction `ix_messages_ticket_created` is declared in. `id` is
        appended as a tiebreaker so two messages written in the same transaction still
        have a deterministic order.

        `include_internal=False` is not merely a convenience: it is what a portal caller
        gets, and it is applied here rather than at the route so that no future caller
        of this method can omit it without passing `True` in as many words.
        """
        criteria = [Message.ticket_id == ticket_id]
        if not include_internal:
            criteria.append(Message.is_internal.is_(False))

        statement = (
            self._select(*criteria)
            .order_by(Message.created_at, Message.id)
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def find_draft(self, ticket_id: uuid.UUID, draft_id: uuid.UUID) -> Message | None:
        """The unsent AI draft `draft_id` names on this ticket, if there is one.

        Three predicates, and all three are the access rule: the id, the ticket the caller has
        already resolved through `require_visible_ticket`, and `sender_type = AI_DRAFT`. A
        message id from another ticket, from another tenant, or belonging to an ordinary reply
        therefore all produce `None`, which the caller renders as one 404.

        **That a non-draft is refused here rather than by a check at the call site is the
        point.** "This message is not a draft" and "there is no such draft" are the same answer
        to a caller outside the tenant, and answering them differently would confirm that a
        guessed message id exists — the oracle ADR-009 refuses.

        The tenant predicate comes from `_select`, so a draft id belonging to another
        organization is unreachable even if the `ticket_id` passed in were somehow wrong.
        """
        result = await self.session.execute(
            self._select(
                Message.id == draft_id,
                Message.ticket_id == ticket_id,
                Message.sender_type == SenderType.AI_DRAFT,
            )
        )
        return result.scalar_one_or_none()
