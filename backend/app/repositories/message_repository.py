"""Message persistence.

The visibility rule is a filter this repository always applies, never one a caller may
forget to request: `list_for_ticket` excludes internal notes unless the caller
explicitly asks for them, and the only caller that asks is one holding
`MESSAGE_READ_INTERNAL`. A note wrongly shown leaks; a note wrongly hidden merely
hides — so the default is the restrictive one.
"""

import uuid
from collections.abc import Sequence

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
