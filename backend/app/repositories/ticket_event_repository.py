"""Ticket event persistence — the append-only activity timeline."""

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement

from app.models.enums import TicketEventType
from app.models.ticket_event import TicketEvent
from app.repositories.base import TenantScopedRepository


class TicketEventRepository(TenantScopedRepository[TicketEvent]):
    """Events within the caller's organization.

    Not row-scoped, for the same reason messages are not: an event is reachable exactly
    when its ticket is, so the scope is applied at `TicketRepository` and this
    repository is only reached afterwards. The tenant filter still applies on its own.

    There is no `update` and no `delete`, and that is not an omission. The table is
    append-only by design — editing a timeline would defeat the point of having one —
    and the base class offers neither, so the constraint is structural rather than a
    convention.
    """

    model = TicketEvent

    async def list_for_ticket(
        self, ticket_id: uuid.UUID, *, include_internal: bool
    ) -> Sequence[TicketEvent]:
        """One ticket's timeline, oldest first.

        Unpaginated: a ticket's history is bounded by how much work was done on it, the
        timeline is read whole (it is the detail screen's activity panel), and a page
        limit would hide the beginning of the story rather than the end of it. Served by
        `ix_ticket_events_ticket_created`.

        `include_internal` excludes `INTERNAL_NOTE_ADDED`. Every other event type is
        about the ticket's own lifecycle, which a customer can already see; that one is
        about something they specifically cannot. Without this filter the two views of
        the same ticket would contradict each other — the thread would hide a note
        entirely while the timeline announced the minute it was written, which is the
        same disclosure the thread's filter exists to prevent, arriving through the side
        door.

        Defaulted to `False` at the call site rather than here, so this method states
        the rule and the service states the capability — and a caller that forgets gets
        the narrow answer.

        **This is a filter on a query, not a capability.** The route is still guarded by
        `TICKET_VIEW`, which every role holds; what varies is which events a role can
        see, and that is decided in `ticket_service.list_events`.
        """
        criteria: list[ColumnElement[bool]] = [TicketEvent.ticket_id == ticket_id]
        if not include_internal:
            criteria.append(TicketEvent.event_type != TicketEventType.INTERNAL_NOTE_ADDED)

        statement = self._select(*criteria).order_by(TicketEvent.created_at, TicketEvent.id)
        result = await self.session.execute(statement)
        return result.scalars().all()
