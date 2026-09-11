"""Domain enumerations.

Persisted as native PostgreSQL enum types so the database rejects invalid values
rather than trusting the application. Python-side `StrEnum` keeps comparisons and
JSON serialization straightforward.

Adding a value later requires an explicit ALTER TYPE in a migration — a deliberate
constraint that makes the schema self-documenting.
"""

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


def pg_enum[E: StrEnum](enum_cls: type[E], name: str) -> SAEnum:
    """Build a native PostgreSQL enum type for `enum_cls`.

    Two details this centralizes, both of which are easy to get wrong per-column:

    `values_callable` — SQLAlchemy persists an enum member's *name* by default, so
    `TicketStatus.OPEN` would be stored as "OPEN". Every value here is lowercase by
    convention, and the check constraints in `tickets` compare against lowercase
    literals, so names would silently mismatch.

    One instance per type — a `CREATE TYPE` is emitted per distinct type object.
    Columns that share a type (ticket priority appears twice on `tickets`) must
    share the object, or DDL tries to create the same type twice.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [member.value for member in e],
    )


class UserRole(StrEnum):
    """Staff and customer roles. Permissions are defined in docs/requirements.md §3."""

    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"
    CUSTOMER = "customer"


class OrganizationPlan(StrEnum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class TicketStatus(StrEnum):
    """Lifecycle states. Permitted transitions live in TICKET_TRANSITIONS below."""

    OPEN = "open"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_CUSTOMER = "waiting_for_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class SenderType(StrEnum):
    """Message authorship.

    `ai_draft` exists so an AI-generated suggestion can never be mistaken for
    human-authored text — required by the spec's AI UX rules (§41).
    """

    CUSTOMER = "customer"
    AGENT = "agent"
    SYSTEM = "system"
    AI_DRAFT = "ai_draft"


class ProcessingStatus(StrEnum):
    """Shared status for asynchronous work (AI analysis, document ingestion)."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DocumentSourceType(StrEnum):
    UPLOAD = "upload"
    URL = "url"
    MANUAL = "manual"


class TicketEventType(StrEnum):
    """Ticket timeline entries. Distinct from AuditAction, which is org-wide."""

    CREATED = "created"
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    STATUS_CHANGED = "status_changed"
    PRIORITY_CHANGED = "priority_changed"
    MESSAGE_ADDED = "message_added"
    INTERNAL_NOTE_ADDED = "internal_note_added"
    ATTACHMENT_ADDED = "attachment_added"
    AI_ANALYSIS_COMPLETED = "ai_analysis_completed"
    SLA_WARNING = "sla_warning"
    SLA_BREACHED = "sla_breached"
    REOPENED = "reopened"


class AuditAction(StrEnum):
    """Auditable actions, per the spec's list (§34)."""

    USER_CREATED = "user_created"
    USER_ROLE_UPDATED = "user_role_updated"
    USER_DEACTIVATED = "user_deactivated"
    TICKET_CREATED = "ticket_created"
    TICKET_ASSIGNED = "ticket_assigned"
    TICKET_PRIORITY_CHANGED = "ticket_priority_changed"
    TICKET_STATUS_CHANGED = "ticket_status_changed"
    TICKET_RESOLVED = "ticket_resolved"
    TICKET_REOPENED = "ticket_reopened"
    KNOWLEDGE_DOCUMENT_CREATED = "knowledge_document_created"
    KNOWLEDGE_DOCUMENT_DELETED = "knowledge_document_deleted"
    AI_ANALYSIS_REQUESTED = "ai_analysis_requested"
    AI_RESPONSE_ACCEPTED = "ai_response_accepted"
    AI_RESPONSE_REGENERATED = "ai_response_regenerated"
    SLA_POLICY_UPDATED = "sla_policy_updated"
    ORGANIZATION_UPDATED = "organization_updated"


class NotificationType(StrEnum):
    TICKET_ASSIGNED = "ticket_assigned"
    TICKET_REASSIGNED = "ticket_reassigned"
    NEW_CUSTOMER_REPLY = "new_customer_reply"
    MENTION = "mention"
    SLA_WARNING = "sla_warning"
    AI_ANALYSIS_COMPLETED = "ai_analysis_completed"
    TICKET_RESOLVED = "ticket_resolved"


class AIOperation(StrEnum):
    """AI operations, tracked per call for cost attribution."""

    CLASSIFY = "classify"
    SENTIMENT = "sentiment"
    SUMMARIZE = "summarize"
    SUGGEST_RESPONSE = "suggest_response"
    EMBED = "embed"
    KNOWLEDGE_ANSWER = "knowledge_answer"


# ---------------------------------------------------------------------------
# Shared column types
# ---------------------------------------------------------------------------

# One instance per database enum type, reused by every column of that type.
# Declaring a second SAEnum under the same name would emit a duplicate CREATE TYPE.
USER_ROLE_ENUM = pg_enum(UserRole, "user_role")
ORGANIZATION_PLAN_ENUM = pg_enum(OrganizationPlan, "organization_plan")
ORGANIZATION_STATUS_ENUM = pg_enum(OrganizationStatus, "organization_status")
TICKET_STATUS_ENUM = pg_enum(TicketStatus, "ticket_status")
TICKET_PRIORITY_ENUM = pg_enum(TicketPriority, "ticket_priority")
SENTIMENT_ENUM = pg_enum(Sentiment, "sentiment")
SENDER_TYPE_ENUM = pg_enum(SenderType, "sender_type")
PROCESSING_STATUS_ENUM = pg_enum(ProcessingStatus, "processing_status")
DOCUMENT_SOURCE_TYPE_ENUM = pg_enum(DocumentSourceType, "document_source_type")
TICKET_EVENT_TYPE_ENUM = pg_enum(TicketEventType, "ticket_event_type")
AUDIT_ACTION_ENUM = pg_enum(AuditAction, "audit_action")
NOTIFICATION_TYPE_ENUM = pg_enum(NotificationType, "notification_type")
AI_OPERATION_ENUM = pg_enum(AIOperation, "ai_operation")


# ---------------------------------------------------------------------------
# Ticket lifecycle
# ---------------------------------------------------------------------------

# Permitted transitions, per docs/requirements.md §5. Defined here rather than in
# the service layer so the rule has exactly one source of truth, and so tests can
# assert against the same table the service enforces.
TICKET_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.OPEN: frozenset({TicketStatus.ASSIGNED}),
    TicketStatus.ASSIGNED: frozenset({TicketStatus.IN_PROGRESS}),
    TicketStatus.IN_PROGRESS: frozenset({TicketStatus.WAITING_FOR_CUSTOMER, TicketStatus.RESOLVED}),
    TicketStatus.WAITING_FOR_CUSTOMER: frozenset({TicketStatus.IN_PROGRESS}),
    TicketStatus.RESOLVED: frozenset({TicketStatus.CLOSED}),
    # Reopen is an explicit action, not a general-purpose status write.
    TicketStatus.CLOSED: frozenset({TicketStatus.OPEN}),
}


def can_transition(current: TicketStatus, target: TicketStatus) -> bool:
    """Whether moving from `current` to `target` is permitted."""
    return target in TICKET_TRANSITIONS.get(current, frozenset())
