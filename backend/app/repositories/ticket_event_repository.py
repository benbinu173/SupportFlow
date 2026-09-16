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
        self, ticket_id: uuid.UUID, *, include_internal: bool, include_sla_alerts: bool
    ) -> Sequence[TicketEvent]:
        """One ticket's timeline, oldest first.

        Unpaginated: a ticket's history is bounded by how much work was done on it, the
        timeline is read whole (it is the detail screen's activity panel), and a page
        limit would hide the beginning of the story rather than the end of it. Served by
        `ix_ticket_events_ticket_created`.

        **Two filters, each one capability, and both narrow.**

        `include_internal` excludes `INTERNAL_NOTE_ADDED`. Every other event type is
        about the ticket's own lifecycle, which a customer can already see; that one is
        about something they specifically cannot. Without this filter the two views of
        the same ticket would contradict each other — the thread would hide a note
        entirely while the timeline announced the minute it was written, which is the
        same disclosure the thread's filter exists to prevent, arriving through the side
        door.

        `include_sla_alerts` excludes `SLA_WARNING` and `SLA_BREACHED`, and it is the
        same argument applied to a second capability. §3's matrix gives `SLA_VIEW` to
        admin, manager, and agent and withholds it from customer, and `sla_service.decorate`
        honours that by leaving `TicketRead.sla` null for a portal caller. A timeline entry
        reading "first response is due 2026-09-16 14:32 UTC" would hand that caller the
        same fact by the other door — and a deadline *is* the policy: a warning at 80% of
        the target states the target, which is exactly what `TicketSLARead`'s docstring
        says a null `sla` is protecting. Two paths to one fact, one closed and one open,
        is the inconsistency ADR-009 exists to prevent.

        Both are passed in from the service rather than read off a context here, so this
        method states the rule and the caller states the capability — and a caller that
        forgets gets the narrow answer.
        """
        criteria: list[ColumnElement[bool]] = [TicketEvent.ticket_id == ticket_id]
        if not include_internal:
            criteria.append(TicketEvent.event_type != TicketEventType.INTERNAL_NOTE_ADDED)
        if not include_sla_alerts:
            criteria.append(
                TicketEvent.event_type.not_in(
                    (TicketEventType.SLA_WARNING, TicketEventType.SLA_BREACHED)
                )
            )

        statement = self._select(*criteria).order_by(TicketEvent.created_at, TicketEvent.id)
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def list_alerts_for_tickets(
        self, ticket_ids: Sequence[uuid.UUID]
    ) -> Sequence[TicketEvent]:
        """The SLA warning and breach entries on these tickets, oldest first.

        Read by `sla_service.decorate` to report `warned_at` and `breached_at` for a page
        of tickets. Batched on purpose: a page is up to a hundred tickets and the
        unbatched version of this is a hundred queries behind one `GET /tickets`.

        Narrowed to `ticket_ids` and not to the whole timeline, so the cost follows the
        page rather than the tenant's history — and narrowed to the two event types for
        the same reason, since every other type is already on the detail screen's feed and
        nothing here would read it.

        The ids come from a page that has already been through the caller's row scope, so
        this does not re-apply it: filtering the events of tickets the caller cannot see
        is not a thing that can happen, because those tickets were never fetched. The
        tenant predicate still applies on its own, from the base class.

        `include_internal` is not a parameter here, and the omission is deliberate rather
        than an oversight: an SLA entry is not an internal note, so that filter does not
        apply to it. The capability that does is `SLA_VIEW`, and it is applied by the
        *caller* — `decorate` returns nothing at all for a caller without it, so there is
        no reachable path to this method that should have filtered and did not. The
        timeline's own copy of the rule is in `list_for_ticket`; that one must filter,
        because the timeline is served to every role.
        """
        if not ticket_ids:
            # `IN ()` is not valid SQL, and an empty page is a real outcome.
            return []

        statement = self._select(
            TicketEvent.ticket_id.in_(ticket_ids),
            TicketEvent.event_type.in_((TicketEventType.SLA_WARNING, TicketEventType.SLA_BREACHED)),
        ).order_by(TicketEvent.created_at, TicketEvent.id)
        result = await self.session.execute(statement)
        return result.scalars().all()
