"""Schema invariant tests.

These run against a real PostgreSQL because the guarantees under test are the
database's, not the ORM's: native enum types, partial indexes, check constraints,
and ON DELETE behaviour. SQLite would silently pass most of them.

What is deliberately covered: the constraints whose failure mode is a correctness
or isolation bug rather than a crash — per-tenant uniqueness, cascade direction,
and the constraints protecting internal notes and AI drafts from reaching a
customer.
"""

import uuid

import pytest
from app.models import (
    AIAnalysis,
    Customer,
    KnowledgeChunk,
    KnowledgeDocument,
    Message,
    Organization,
    SLAPolicy,
    Ticket,
    User,
)
from app.models.enums import (
    AIOperation,
    DocumentSourceType,
    ProcessingStatus,
    SenderType,
    TicketPriority,
    TicketStatus,
    UserRole,
)
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
# Flushed rather than committed, so each returns a row with a database-generated
# id while staying inside the test's rolled-back transaction.


async def make_org(db: AsyncSession, slug: str | None = None) -> Organization:
    org = Organization(name="Acme Support", slug=slug or f"acme-{uuid.uuid4().hex[:8]}")
    db.add(org)
    await db.flush()
    return org


async def make_user(
    db: AsyncSession,
    org: Organization,
    email: str = "agent@example.com",
    role: UserRole = UserRole.AGENT,
) -> User:
    user = User(
        organization_id=org.id,
        name="Test Agent",
        email=email,
        password_hash="$argon2id$v=19$not-a-real-hash",
        role=role,
    )
    db.add(user)
    await db.flush()
    return user


async def make_customer(
    db: AsyncSession, org: Organization, email: str = "buyer@example.com"
) -> Customer:
    customer = Customer(organization_id=org.id, name="Test Buyer", email=email)
    db.add(customer)
    await db.flush()
    return customer


async def make_ticket(
    db: AsyncSession,
    org: Organization,
    customer: Customer,
    number: int = 1,
    **kwargs: object,
) -> Ticket:
    ticket = Ticket(
        organization_id=org.id,
        customer_id=customer.id,
        number=number,
        subject="Cannot log in",
        description="Password reset email never arrives.",
        **kwargs,
    )
    db.add(ticket)
    await db.flush()
    return ticket


# ---------------------------------------------------------------------------
# Enum persistence
# ---------------------------------------------------------------------------


async def test_enums_persist_as_lowercase_values(db: AsyncSession) -> None:
    """Values, not member names, reach the database.

    SQLAlchemy stores an enum member's *name* by default, which would write "OPEN"
    and break the lowercase literals in the ticket check constraints and partial
    indexes. Asserted through raw SQL, since the ORM would map either form back to
    the same member and hide the difference.
    """
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    stored = await db.execute(
        text("SELECT status::text, priority::text FROM tickets WHERE id = :id"),
        {"id": ticket.id},
    )
    assert stored.one() == ("open", "medium")


async def test_enum_rejects_unknown_value(db: AsyncSession) -> None:
    """The database, not just Pydantic, refuses an invalid status."""
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    with pytest.raises(Exception, match="invalid input value for enum"):
        await db.execute(
            text("UPDATE tickets SET status = 'escalated' WHERE id = :id"),
            {"id": ticket.id},
        )


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


async def test_same_email_allowed_in_different_organizations(db: AsyncSession) -> None:
    """Uniqueness is per tenant.

    A global unique index would both block a legitimate signup and leak that an
    address already exists in someone else's organization.
    """
    first = await make_org(db)
    second = await make_org(db)

    await make_user(db, first, email="shared@example.com")
    await make_user(db, second, email="shared@example.com")

    count = await db.scalar(
        select(text("count(*)")).select_from(User).where(User.email == "shared@example.com")
    )
    assert count == 2


async def test_duplicate_email_within_organization_rejected(db: AsyncSession) -> None:
    org = await make_org(db)
    await make_user(db, org, email="dupe@example.com")

    with pytest.raises(IntegrityError):
        await make_user(db, org, email="dupe@example.com")


async def test_ticket_number_unique_per_organization(db: AsyncSession) -> None:
    """Two tenants may both have ticket #1; one tenant may not.

    This index is what turns the service layer's number allocation race into a
    retryable error instead of two tickets sharing a reference.
    """
    first = await make_org(db)
    second = await make_org(db)
    first_customer = await make_customer(db, first)
    second_customer = await make_customer(db, second)

    await make_ticket(db, first, first_customer, number=1)
    await make_ticket(db, second, second_customer, number=1)

    with pytest.raises(IntegrityError):
        await make_ticket(db, first, first_customer, number=1)


async def test_sla_policy_unique_per_priority(db: AsyncSession) -> None:
    """One policy per priority, so lookup never has to break a tie."""
    org = await make_org(db)
    db.add(
        SLAPolicy(
            organization_id=org.id,
            priority=TicketPriority.HIGH,
            response_time_minutes=30,
            resolution_time_minutes=240,
        )
    )
    await db.flush()

    db.add(
        SLAPolicy(
            organization_id=org.id,
            priority=TicketPriority.HIGH,
            response_time_minutes=15,
            resolution_time_minutes=120,
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


# ---------------------------------------------------------------------------
# Delete behaviour
# ---------------------------------------------------------------------------


async def test_deleting_organization_cascades_to_tenant_data(db: AsyncSession) -> None:
    """Removing a tenant removes its rows rather than orphaning them.

    Exercised with raw SQL so the database's ON DELETE CASCADE is what is proven,
    not the ORM's cascade, which would not apply to rows it had not loaded.
    """
    org = await make_org(db)
    customer = await make_customer(db, org)
    await make_user(db, org)
    await make_ticket(db, org, customer)

    await db.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": org.id})

    for table in ("users", "customers", "tickets"):
        remaining = await db.scalar(
            text(f"SELECT count(*) FROM {table} WHERE organization_id = :id"),  # noqa: S608
            {"id": org.id},
        )
        assert remaining == 0, f"{table} rows survived tenant deletion"


async def test_deleting_agent_preserves_their_tickets(db: AsyncSession) -> None:
    """A deactivated or deleted agent must not take ticket history with them."""
    org = await make_org(db)
    customer = await make_customer(db, org)
    agent = await make_user(db, org)
    ticket = await make_ticket(db, org, customer, assigned_agent_id=agent.id)

    await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": agent.id})

    assigned = await db.scalar(
        text("SELECT assigned_agent_id FROM tickets WHERE id = :id"), {"id": ticket.id}
    )
    assert assigned is None


async def test_deleting_document_removes_its_chunks(db: AsyncSession) -> None:
    """Chunks are derived data with no meaning once the document is gone."""
    org = await make_org(db)
    document = KnowledgeDocument(
        organization_id=org.id,
        title="Refund policy",
        content="Refunds are issued within 14 days.",
        source_type=DocumentSourceType.MANUAL,
    )
    db.add(document)
    await db.flush()

    db.add(
        KnowledgeChunk(
            organization_id=org.id,
            document_id=document.id,
            chunk_index=0,
            content="Refunds are issued within 14 days.",
            token_count=9,
        )
    )
    await db.flush()

    await db.execute(text("DELETE FROM knowledge_documents WHERE id = :id"), {"id": document.id})
    remaining = await db.scalar(
        text("SELECT count(*) FROM knowledge_chunks WHERE document_id = :id"),
        {"id": document.id},
    )
    assert remaining == 0


# ---------------------------------------------------------------------------
# Check constraints
# ---------------------------------------------------------------------------


async def test_internal_note_cannot_be_authored_by_customer(db: AsyncSession) -> None:
    """Staff-only commentary is structurally unable to carry a customer sender."""
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    db.add(
        Message(
            organization_id=org.id,
            ticket_id=ticket.id,
            sender_type=SenderType.CUSTOMER,
            body="internal",
            is_internal=True,
        )
    )
    with pytest.raises(IntegrityError, match="internal_note_not_from_customer"):
        await db.flush()


async def test_ai_draft_cannot_be_customer_visible(db: AsyncSession) -> None:
    """An unreviewed AI draft can never be published to the customer thread.

    The spec forbids AI output reaching a customer unreviewed; this makes that a
    schema guarantee rather than a service-layer convention.
    """
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    db.add(
        Message(
            organization_id=org.id,
            ticket_id=ticket.id,
            sender_type=SenderType.AI_DRAFT,
            body="Suggested reply",
            is_internal=False,
        )
    )
    with pytest.raises(IntegrityError, match="ai_draft_is_internal"):
        await db.flush()


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
async def test_confidence_outside_unit_range_rejected(db: AsyncSession, confidence: float) -> None:
    """Confidence is a probability; a value outside [0,1] is a provider parsing bug."""
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    db.add(
        AIAnalysis(
            organization_id=org.id,
            ticket_id=ticket.id,
            operation=AIOperation.CLASSIFY,
            status=ProcessingStatus.COMPLETED,
            result={"category": "billing"},
            confidence=confidence,
            provider="anthropic",
            model="claude-opus-5",
        )
    )
    with pytest.raises(IntegrityError, match="confidence_range"):
        await db.flush()


async def test_completed_analysis_must_carry_a_result(db: AsyncSession) -> None:
    """A completed row with no payload is indistinguishable from a lost result."""
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    db.add(
        AIAnalysis(
            organization_id=org.id,
            ticket_id=ticket.id,
            operation=AIOperation.SUMMARIZE,
            status=ProcessingStatus.COMPLETED,
            result=None,
            provider="anthropic",
            model="claude-opus-5",
        )
    )
    with pytest.raises(IntegrityError, match="terminal_status_has_payload"):
        await db.flush()


async def test_json_null_does_not_satisfy_a_required_result(db: AsyncSession) -> None:
    """JSONB 'null' is a value, not an absence, and must not pass as a result.

    Regression test. SQLAlchemy's JSON types persist Python None as JSON `null` by
    default, which satisfies `IS NOT NULL` — so a completed analysis with no
    payload once passed the constraint, and read back as None through the ORM,
    leaving nothing to notice from Python. Written as raw SQL because that is the
    only remaining way to attempt it.
    """
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    with pytest.raises(IntegrityError, match="terminal_status_has_payload"):
        await db.execute(
            text(
                "INSERT INTO ai_analyses "
                "(organization_id, ticket_id, operation, status, result, provider, model) "
                "VALUES (:org, :ticket, 'summarize', 'completed', 'null'::jsonb, 'p', 'm')"
            ),
            {"org": org.id, "ticket": ticket.id},
        )


async def test_none_result_is_stored_as_sql_null(db: AsyncSession) -> None:
    """A pending analysis has a genuinely absent result, not JSON `null`.

    The other half of the regression: `none_as_null` has to be in effect, or
    `result IS NULL` is false for every row and any query filtering on it silently
    returns nothing.
    """
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    analysis = AIAnalysis(
        organization_id=org.id,
        ticket_id=ticket.id,
        operation=AIOperation.CLASSIFY,
        status=ProcessingStatus.PENDING,
        provider="anthropic",
        model="claude-opus-5",
    )
    db.add(analysis)
    await db.flush()

    is_sql_null = await db.scalar(
        text("SELECT result IS NULL FROM ai_analyses WHERE id = :id"), {"id": analysis.id}
    )
    assert is_sql_null is True


async def test_resolution_target_cannot_precede_response_target(db: AsyncSession) -> None:
    """An unsatisfiable SLA — breaching resolution while still on time to reply."""
    org = await make_org(db)
    db.add(
        SLAPolicy(
            organization_id=org.id,
            priority=TicketPriority.LOW,
            response_time_minutes=480,
            resolution_time_minutes=60,
        )
    )
    with pytest.raises(IntegrityError, match="resolution_after_response"):
        await db.flush()


async def test_resolved_ticket_requires_resolved_at(db: AsyncSession) -> None:
    """SLA math reads `resolved_at`; a resolved ticket without one breaks reporting."""
    org = await make_org(db)
    customer = await make_customer(db, org)

    with pytest.raises(IntegrityError, match="resolved_at_matches_status"):
        await make_ticket(db, org, customer, status=TicketStatus.RESOLVED)


# ---------------------------------------------------------------------------
# pgvector
# ---------------------------------------------------------------------------


async def test_embedding_round_trips_and_orders_by_cosine_distance(db: AsyncSession) -> None:
    """Vectors store and compare, which is what the HNSW index is built for."""
    from app.models import EMBEDDING_DIMENSIONS

    org = await make_org(db)
    document = KnowledgeDocument(
        organization_id=org.id,
        title="Shipping",
        content="Orders ship in two days.",
        source_type=DocumentSourceType.MANUAL,
    )
    db.add(document)
    await db.flush()

    near = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
    far = [0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0]
    for index, vector in enumerate((near, far)):
        db.add(
            KnowledgeChunk(
                organization_id=org.id,
                document_id=document.id,
                chunk_index=index,
                content=f"chunk {index}",
                embedding=vector,
                embedding_model="text-embedding-3-small",
                token_count=2,
            )
        )
    await db.flush()

    # `<=>` is cosine distance, matching the index's vector_cosine_ops opclass.
    ordered = await db.execute(
        select(KnowledgeChunk.chunk_index)
        .where(KnowledgeChunk.document_id == document.id)
        .order_by(KnowledgeChunk.embedding.cosine_distance(near))
    )
    assert list(ordered.scalars()) == [0, 1]


async def test_chunk_embedding_may_be_absent(db: AsyncSession) -> None:
    """A failed embedding call leaves a retryable row, not lost text."""
    org = await make_org(db)
    document = KnowledgeDocument(
        organization_id=org.id,
        title="Returns",
        content="Returns accepted within 30 days.",
        source_type=DocumentSourceType.UPLOAD,
    )
    db.add(document)
    await db.flush()

    chunk = KnowledgeChunk(
        organization_id=org.id,
        document_id=document.id,
        chunk_index=0,
        content="Returns accepted within 30 days.",
        token_count=7,
    )
    db.add(chunk)
    await db.flush()

    assert chunk.embedding is None
    assert chunk.embedding_model is None


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


async def test_new_ticket_defaults_to_open_and_medium(db: AsyncSession) -> None:
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    assert ticket.status is TicketStatus.OPEN
    assert ticket.priority is TicketPriority.MEDIUM
    assert ticket.ai_recommended_priority is None


async def test_every_tenant_table_is_indexed_on_organization_id(
    db: AsyncSession,
) -> None:
    """The metadata invariant holds in PostgreSQL, not just in the model classes.

    Queried against `pg_index` rather than trusting `Table.indexes`, because the
    mixin's `__org_index__` opt-out is only meaningful if the emitted DDL agrees.
    A `UNIQUE` constraint counts: PostgreSQL backs one with a real unique B-tree, so
    the per-tenant email constraints serve an org-only predicate.
    """
    unindexed = await db.scalars(
        text(
            """
            SELECT t.relname
            FROM pg_class t
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public' AND t.relkind = 'r'
              AND t.relname <> 'organizations'
              AND EXISTS (
                SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = t.oid AND a.attname = 'organization_id' AND a.attnum > 0)
              AND NOT EXISTS (
                SELECT 1 FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                WHERE i.indrelid = t.oid AND a.attname = 'organization_id')
            """
        )
    )
    assert list(unindexed) == []


async def test_message_defaults_to_customer_visible(db: AsyncSession) -> None:
    """`is_internal` defaults false, so the flag is always an explicit decision."""
    org = await make_org(db)
    customer = await make_customer(db, org)
    ticket = await make_ticket(db, org, customer)

    message = Message(
        organization_id=org.id,
        ticket_id=ticket.id,
        sender_type=SenderType.AGENT,
        body="Looking into it.",
    )
    db.add(message)
    await db.flush()

    assert message.is_internal is False


async def test_document_is_unpublished_until_reviewed(db: AsyncSession) -> None:
    """Ingestion must not expose a document to retrieval before review."""
    org = await make_org(db)
    document = KnowledgeDocument(
        organization_id=org.id,
        title="Draft article",
        content="...",
        source_type=DocumentSourceType.URL,
        source_reference="https://example.com/help",
    )
    db.add(document)
    await db.flush()

    assert document.is_published is False
    assert document.status is ProcessingStatus.PENDING
    assert document.chunk_count == 0
