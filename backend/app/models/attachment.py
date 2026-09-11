"""Attachment — a file uploaded against a ticket."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.message import Message
    from app.models.organization import Organization
    from app.models.ticket import Ticket
    from app.models.user import User


class Attachment(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """Metadata for a stored file.

    The file itself lives in object storage; this row holds only the key and the
    facts needed to serve it. Downloads go through the application rather than a
    public bucket URL, so tenant scoping applies to files as it does to rows.
    """

    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_attachments_ticket_created", "ticket_id", "created_at"),
        # Every attachment belongs to a ticket; `message_id` narrows it to a
        # specific message when the upload accompanied one.
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Set when the file arrived with a message rather than on its own. CASCADE would
    # be wrong here: an attachment outlives the message it was attached to.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        index=True,
    )

    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    # As supplied by the client. Displayed, never used to build a filesystem path:
    # it is attacker-controlled and may contain traversal sequences.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)

    # Object storage key. Server-generated and unique, which is what decouples
    # stored objects from the untrusted display name above.
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)

    # Detected server-side, not taken from the upload's Content-Type header.
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)

    # BigInteger: an Integer column caps at ~2GB and would overflow rather than
    # reject an oversized upload.
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    organization: Mapped["Organization"] = relationship()
    ticket: Mapped["Ticket"] = relationship(back_populates="attachments")
    message: Mapped["Message | None"] = relationship(back_populates="attachments")
    uploaded_by: Mapped["User | None"] = relationship()

    def __repr__(self) -> str:
        return f"<Attachment {self.filename} ({self.size_bytes} bytes)>"
