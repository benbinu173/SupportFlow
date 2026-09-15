"""Attachment persistence, and the one visibility rule an attachment has.

Not row-scoped, deliberately — the same reasoning as `MessageRepository`, and ADR-019
records it. `attachments` has no `customer_id` and no `assigned_agent_id`: an attachment
belongs to a ticket, and `row_scope_predicate` needs columns to compare against the
caller's identity. So `ATTACHMENT_SCOPE_BY_ROLE` — which *is* `TICKET_SCOPE_BY_ROLE` and
matches §3's matrix row for row — is applied at the ticket, by the caller resolving the
ticket through `ticket_service.require_visible_ticket` before this repository is reached.
That is one implementation of `OWN`/`ASSIGNED` rather than two that can drift.

What is left for this repository is the rule the ticket scope cannot express: an
attachment that arrived with an **internal note** is internal too. The customer who owns
the ticket holds `ATTACHMENT_DOWNLOAD` and reaches the ticket, so a ticket-level check
lets them straight through to a file that was attached to a note they cannot read.

The filter is applied the same way `MessageRepository` applies its own: as the default,
with an explicit `include_internal=True` required to see past it. A note wrongly shown
leaks; a note wrongly hidden merely hides.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, select

from app.models.attachment import Attachment
from app.models.message import Message
from app.repositories.base import TenantScopedRepository


class AttachmentRepository(TenantScopedRepository[Attachment]):
    """Attachments within the caller's organization, narrowed by internal visibility.

    The tenant filter still applies independently of all of the above. An attachment id
    from another organization finds nothing here even if a bug upstream handed this
    repository the wrong `ticket_id`.
    """

    model = Attachment

    def _not_internal(self) -> ColumnElement[bool]:
        """The predicate that excludes files attached to an internal note.

        An `EXISTS` rather than a join, so the criteria compose with the others without
        changing the shape of the query or introducing duplicate rows. A `LEFT JOIN`
        would also work and would be one more thing to get wrong when a second join
        arrives.

        An attachment with `message_id IS NULL` is kept: it belongs to the ticket rather
        than to a message, and there is no note for it to be internal to.
        """
        return ~(
            select(Message.id)
            .where(Message.id == Attachment.message_id, Message.is_internal.is_(True))
            .exists()
        )

    async def list_for_ticket(
        self, ticket_id: uuid.UUID, *, include_internal: bool, limit: int, offset: int = 0
    ) -> Sequence[Attachment]:
        """One ticket's attachments, oldest first.

        Ascending because attachments accumulate in the order they were sent and read
        as a list under the conversation, matching `messages`. `id` is appended as a
        tiebreak so two files uploaded in the same transaction still have a
        deterministic order — the same reason the ticket queue carries one, though here
        it only affects display rather than pagination correctness.

        `include_internal=False` is what a portal caller gets, and it is applied here
        rather than at the route so no future caller of this method can omit it without
        passing `True` in as many words.
        """
        criteria: list[ColumnElement[bool]] = [Attachment.ticket_id == ticket_id]
        if not include_internal:
            criteria.append(self._not_internal())

        statement = (
            self._select(*criteria)
            .order_by(Attachment.created_at, Attachment.id)
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def get_visible(
        self, attachment_id: uuid.UUID, *, include_internal: bool
    ) -> Attachment | None:
        """One attachment, if the caller may reach it at all.

        Returns `None` for an internal-note attachment when `include_internal` is false,
        which the service turns into the **same 404** a missing attachment gets. That is
        deliberate and matches ADR-009: a customer who guesses an id must not be able to
        tell "that file exists but is not for you" from "that file does not exist",
        because the first answer confirms a file they were never meant to know about.

        Named `get_visible` rather than overriding `get`, for the same reason
        `TicketRepository` does it: the scoped read must not be mistakable for the
        tenant-only one.
        """
        criteria: list[ColumnElement[bool]] = [Attachment.id == attachment_id]
        if not include_internal:
            criteria.append(self._not_internal())

        result = await self.session.execute(self._select(*criteria))
        return result.scalar_one_or_none()
