"""KnowledgeChunk — an embedded passage used for semantic retrieval."""

import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.knowledge_document import KnowledgeDocument
    from app.models.organization import Organization

# Matches text-embedding-3-small. Fixed at the column level because pgvector
# requires a declared dimension, so changing embedding models is a migration and
# a re-embed, not a config edit (ADR-008).
EMBEDDING_DIMENSIONS = 1536


class KnowledgeChunk(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A passage of a document plus its embedding.

    Documents are split because embedding models have a token limit and because
    retrieval precision degrades when a whole article is reduced to one vector.
    Chunks carry `organization_id` directly rather than joining through the
    document: a vector search filtered by tenant must not require a join to be
    safe, and the index below depends on the column being local.
    """

    __tablename__ = "knowledge_chunks"
    # ix_knowledge_chunks_org_document below leads with organization_id.
    __org_index__ = False
    __table_args__ = (
        # Approximate nearest-neighbour search over cosine distance. HNSW rather
        # than IVFFlat: it needs no training step and stays accurate as rows are
        # added incrementally, which matches ingestion happening continuously.
        #
        # The operator class must match the query operator — `vector_cosine_ops`
        # here means retrieval has to use `<=>`, or the index is ignored.
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # Tenant-scoped pre-filter. Postgres can combine this with the HNSW scan;
        # without it, a filtered search degrades to scanning another tenant's
        # neighbours and discarding them.
        Index("ix_knowledge_chunks_org_document", "organization_id", "document_id"),
        # Chunk order within a document is meaningful — adjacent chunks are used to
        # widen context around a hit — and must be unambiguous.
        UniqueConstraint("document_id", "chunk_index"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint("token_count > 0", name="token_count_positive"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Position within the document, zero-based.
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Nullable: a chunk row is written before the embedding call returns, so a
    # provider failure leaves a retryable row rather than losing the split text.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))

    # Which model produced the vector. Mixing embedding models in one index yields
    # meaningless distances, so this is what makes a stale-vector sweep possible
    # after a model change.
    embedding_model: Mapped[str | None] = mapped_column(String(100))

    token_count: Mapped[int] = mapped_column(Integer, nullable=False)

    organization: Mapped["Organization"] = relationship()
    document: Mapped["KnowledgeDocument"] = relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return f"<KnowledgeChunk {self.document_id}#{self.chunk_index}>"
