"""Notification — an in-app alert for a user."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, UUIDPrimaryKeyMixin
from app.models.enums import NOTIFICATION_TYPE_ENUM, NotificationType

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.ticket import Ticket
    from app.models.user import User


class Notification(UUIDPrimaryKeyMixin, OrganizationScopedMixin, Base):
    """A single alert addressed to one user.

    Persisted rather than only pushed over the websocket, because a user who was
    offline when the event fired must still see it on next login. The websocket is
    the delivery optimization; this table is the source of truth.

    `read_at` doubles as the read flag — a nullable timestamp carries strictly more
    information than a boolean, and there is no state where "read" is true but the
    time is unknown.

    `emailed_at` is the same idiom for a different question. The notification row is
    written inside the request's transaction; the *email* is sent later, by a worker,
    and Celery configured with `acks_late` is at-least-once — a worker killed after
    sending but before acknowledging would be handed the same task again. This column
    is what the task checks before sending, narrowing "any redelivery duplicates mail"
    to "a crash between the send and this mark does". It also gives a future backlog
    sweep a query: `WHERE emailed_at IS NULL` is exactly the set of notifications whose
    delivery never happened.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # The unread badge and dropdown, which is the only hot read. Partial,
        # because read notifications accumulate indefinitely and are never counted.
        Index(
            "ix_notifications_user_unread",
            "user_id",
            "created_at",
            postgresql_where=text("read_at IS NULL"),
        ),
        # Full history view, and the retention purge.
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    # The recipient. CASCADE: notifications are per-user state with no value once
    # the account is gone, unlike audit entries.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    notification_type: Mapped[NotificationType] = mapped_column(
        NOTIFICATION_TYPE_ENUM, nullable=False
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)

    # The ticket this concerns, when there is one. Nullable because not every
    # notification type is ticket-scoped, and CASCADE because an alert pointing at
    # a deleted ticket would be a dead link.
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        index=True,
    )

    # NULL means unread.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # NULL means the email has not gone out. Set only *after* a successful send, so a
    # failed attempt leaves the row indistinguishable from one that was never queued —
    # which is correct: neither has been delivered, and the retry should pick both up.
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    organization: Mapped["Organization"] = relationship()
    user: Mapped["User"] = relationship()
    ticket: Mapped["Ticket | None"] = relationship()

    def __repr__(self) -> str:
        state = "read" if self.read_at else "unread"
        return f"<Notification {self.notification_type} ({state})>"
