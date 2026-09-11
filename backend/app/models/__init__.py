"""SQLAlchemy models.

Every model is imported here for one specific reason: Alembic autogenerate
compares the database against `Base.metadata`, and a model class only registers
itself there when its module is imported. A model missing from this file produces
migrations that silently omit its table.

`app/alembic/env.py` imports this package for exactly that effect.
"""

from app.models.ai_analysis import AIAnalysis
from app.models.ai_usage import AIUsage
from app.models.attachment import Attachment
from app.models.audit_log import AuditLog
from app.models.base import (
    Base,
    OrganizationScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.models.customer import Customer
from app.models.knowledge_chunk import EMBEDDING_DIMENSIONS, KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.message import Message
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.refresh_token import RefreshToken
from app.models.sla_policy import SLAPolicy
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import User

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "AIAnalysis",
    "AIUsage",
    "Attachment",
    "AuditLog",
    "Base",
    "Customer",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "Message",
    "Notification",
    "Organization",
    "OrganizationScopedMixin",
    "RefreshToken",
    "SLAPolicy",
    "Ticket",
    "TicketEvent",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "User",
]
