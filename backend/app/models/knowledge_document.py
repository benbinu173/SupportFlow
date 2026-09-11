"""KnowledgeDocument — a source document for retrieval-augmented answers."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    DOCUMENT_SOURCE_TYPE_ENUM,
    PROCESSING_STATUS_ENUM,
    DocumentSourceType,
    ProcessingStatus,
)

if TYPE_CHECKING:
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.organization import Organization
    from app.models.user import User


class KnowledgeDocument(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A knowledge-base article, upload, or crawled page.

    Ingestion is asynchronous: a row is created immediately with
    `status=pending`, then a worker chunks and embeds it. `status` is therefore the
    document's own processing state, separate from `is_published`, which is the
    editorial decision about whether agents should see it at all.
    """

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        # Retrieval reads published documents only, and a tenant's unpublished
        # drafts should not bloat the index.
        Index(
            "ix_knowledge_documents_org_published",
            "organization_id",
            "updated_at",
            postgresql_where=text("is_published = true AND status = 'completed'"),
        ),
        # Ingestion queue and retry sweep.
        Index(
            "ix_knowledge_documents_pending",
            "organization_id",
            "created_at",
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
        # Title search in the admin list view.
        Index(
            "ix_knowledge_documents_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)

    # Full extracted text. Retained after chunking so the document can be
    # re-chunked when the strategy or embedding model changes, without re-fetching
    # the original source.
    content: Mapped[str] = mapped_column(Text, nullable=False)

    source_type: Mapped[DocumentSourceType] = mapped_column(
        DOCUMENT_SOURCE_TYPE_ENUM, nullable=False
    )

    # The URL or object-storage key this came from. NULL for manually authored
    # articles, which have no external source.
    source_reference: Mapped[str | None] = mapped_column(String(1000))

    status: Mapped[ProcessingStatus] = mapped_column(
        PROCESSING_STATUS_ENUM,
        nullable=False,
        default=ProcessingStatus.PENDING,
        server_default=ProcessingStatus.PENDING.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    # Denormalized count, maintained by the ingestion worker. Cheap to read in list
    # views that would otherwise aggregate over the chunk table per row.
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # Editorial gate, independent of processing state. Defaults to false so a
    # freshly ingested document is not exposed to retrieval before review.
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped["Organization"] = relationship(back_populates="knowledge_documents")
    created_by: Mapped["User | None"] = relationship()

    # Chunks are derived data: deleting the document must delete them, and
    # re-ingesting replaces them wholesale.
    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="KnowledgeChunk.chunk_index",
    )

    def __repr__(self) -> str:
        return f"<KnowledgeDocument {self.title!r} ({self.status})>"
