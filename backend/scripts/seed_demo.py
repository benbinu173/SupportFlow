"""Demo seed data — §56's "Acme Support", built by walking the product's own service layer.

    cd backend && .venv/Scripts/python.exe -m scripts.seed_demo
    .venv/Scripts/python.exe -m scripts.seed_demo --reset

**Invoked with `-m`, and that is not a style preference.** `python scripts/seed_demo.py` puts
`scripts/` on `sys.path` rather than the project root, so `import app` resolves only when the
project happens to be installed editable — true in a developer's venv, false in the production
image, which installs dependencies and reaches `app/` through `COPY` alone. There the path form
dies with `ModuleNotFoundError: No module named 'app'`, which is how this was found. `-m` from the
project root puts the root on `sys.path`, which is the one thing both environments guarantee.
`make seed` and `make prod-seed` use this form; the deployment runs it inside the container,
because the production database publishes no port.

**It writes through `app/services/`, not through `INSERT`s**, and that is the decision the rest
of this file follows from. A seed that inserted rows directly would produce data the application
never would: no audit trail, no ticket events, no SLA timers started, no notifications staged —
a demo that looks populated and proves nothing. Going through the services means the seeded
tenant is *indistinguishable* from one a person built by hand, because it was built by the same
code. The single exception is the backdating step at the end, documented where it happens; it is
the one thing the service layer cannot express.

**The tickets are queued for analysis and have no results, which is §60's line exactly.**
`create_ticket` calls `ai_analysis_service.request_analysis` itself — §18's steps 1 and 2, and
its comment says the quiet part: *"the ticket is queued for analysis, and every new ticket is"* —
so this script never touches AI. The thirty tickets arrive with sixty `ai_analyses` rows, a
`classify` and a `sentiment` each, every one `pending` and every one empty. That is the real
pre-worker state of the queue rather than a gap in the seed, and a worker with `AI_API_KEY`
configured fills it without this script's help. What §60 forbids is *fabricated* results, and
there are none: `sentiment`, `ai_recommended_priority`, and the summaries are null because no
model computed them, not because a seeder wrote zeroes into them.

Category and priority **are** set, and both are legitimate: `TicketCreate` accepts them, so they
are what the person raising the ticket declared (§5) rather than anything a model produced.

**Knowledge documents are registered but not ingested, without an embedding key.** `create_manual`
commits the row and queues the worker, and the worker needs `EMBEDDING_API_KEY` to produce
vectors. Without one the documents sit in `pending` — visible, deletable, not yet searchable —
which is the truth about a deployment with embeddings unconfigured, and is why this script says
so at the end rather than reporting a working knowledge base it did not build.

**The account addresses are `@acme.example.com`, not §56's `@acme.local`.** `.local` is a
special-use TLD that EmailStr refuses outright — correctly, since no mail could ever reach it —
so the spec's spelling cannot pass this project's own validation. Rather than weaken the
validator to accommodate demo data, the seed keeps the local parts §56 names (`admin`, `manager`,
`agent1`, `agent2`) on a domain the validator accepts.
"""

# This script reports to a person at a terminal, which is what `print` is for. The rest of the
# codebase logs; a seeding summary is not a log line.
# ruff: noqa: T201

import argparse
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import SessionFactory
from app.core.event_loop import run
from app.core.exceptions import AppError
from app.core.tenancy import TenantContext
from app.models.ai_analysis import AIAnalysis
from app.models.enums import TicketPriority, TicketStatus, UserRole
from app.models.knowledge_document import KnowledgeDocument
from app.models.message import Message
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.schemas.customer import CustomerCreate
from app.schemas.knowledge import KnowledgeDocumentCreate
from app.schemas.message import MessageCreate
from app.schemas.ticket import TicketCreate, TicketStatusUpdate
from app.schemas.user import UserCreate
from app.services import (
    auth_service,
    customer_service,
    knowledge_service,
    message_service,
    ticket_service,
    user_service,
)

# --------------------------------------------------------------------------
# The demo tenant, spelled out
# --------------------------------------------------------------------------

ORGANIZATION_NAME = "Acme Support"
SLUG = "acme-support"

# One password for every demo account. Long enough for `PASSWORD_MIN_LENGTH` and obviously a
# demo credential, which is the point: it is published in the README, so it must never be
# mistaken for a secret that matters.
PASSWORD = "DemoPassw0rd!23"

ADMIN = ("Dana Whitfield", "admin@acme.example.com")
MANAGER = ("Marco Ferreira", "manager@acme.example.com")
AGENTS = [
    ("Priya Raman", "agent1@acme.example.com"),
    ("Tom Okafor", "agent2@acme.example.com"),
]

CUSTOMERS = [
    ("Helena Marsh", "helena.marsh@northwind.example.com", "+44 20 7946 0011"),
    ("Yusuf Demir", "yusuf.demir@brightlayer.example.com", "+44 20 7946 0022"),
    ("Ana Costa", "ana.costa@vantage.example.com", "+1 415 555 0134"),
    ("Ravi Menon", "ravi.menon@lumenlabs.example.com", "+1 415 555 0177"),
    ("Greta Lindqvist", "greta.lindqvist@nordkraft.example.com", "+46 8 555 0199"),
    ("Owen Blake", "owen.blake@harborline.example.com", None),
    ("Mei Zhang", "mei.zhang@quaystone.example.com", "+61 2 5550 0142"),
    ("Diego Alvarez", "diego.alvarez@pampasoft.example.com", None),
]

# The five documents §56 asks for, written to be genuinely answerable — a question about one of
# these subjects has a sentence in here that supports an answer, which is what makes the RAG
# walkthrough meaningful rather than decorative.
DOCUMENTS = [
    (
        "Shipping policy",
        """# Shipping policy

Standard shipping takes three to five business days and is free on orders over 50 GBP.
Express shipping arrives on the next working day for a flat fee of 12 GBP.

We ship to the United Kingdom, the European Union, and Australia. Orders placed after
14:00 UTC are dispatched the following working day.

Tracking numbers are emailed as soon as the carrier scans the parcel. If a tracking
number has not arrived within 48 hours of dispatch, contact the support desk and we will
trace it with the carrier.
""",
    ),
    (
        "Returns and refunds policy",
        """# Returns and refunds policy

Customers may return an item within 30 days of delivery for a full refund.

Refunds are processed within five working days of approval, to the original payment
method. The refund is approved once the returned item reaches our warehouse and passes
inspection.

Return shipping is free for faulty goods. For a change of mind, the customer pays return
shipping and we refund the original order value only.

Items that have been used, damaged by the customer, or returned without their original
packaging may be refused or refunded at a reduced value.
""",
    ),
    (
        "Support hours and response targets",
        """# Support hours

The support desk answers messages from 08:00 to 18:00 UTC, Monday to Friday.

Outside those hours, messages are queued and answered on the next working day. Urgent
issues affecting a production system are monitored at weekends.

Our response targets are four working hours for urgent tickets, one working day for high
priority, two working days for medium, and five working days for low priority.
""",
    ),
    (
        "Warranty coverage",
        """# Warranty coverage

All hardware carries a 24-month warranty from the date of delivery.

The warranty covers manufacturing defects and component failure under normal use. It does
not cover accidental damage, liquid ingress, or normal cosmetic wear such as scratches.

To make a warranty claim, send the order number, a description of the fault, and
photographs where relevant. Approved claims are repaired or replaced at our discretion,
and return shipping for a warranty claim is always paid by us.
""",
    ),
    (
        "Account and billing FAQ",
        """# Account and billing FAQ

**How do I change my plan?** Upgrade or downgrade at any time from the account settings
page. Upgrades take effect immediately and are billed pro rata. Downgrades take effect at
the end of the current billing period.

**Where do I find my invoices?** Every invoice is available under Billing in the account
settings page, and is emailed to the billing contact when it is issued.

**How do I reset my password?** Use the "Forgot password" link on the sign-in page. The
reset link is valid for one hour.

**Can I add more users?** Yes. The number of seats depends on your plan: Starter includes
3 seats, Growth includes 10, and Enterprise is unlimited.
""",
    ),
]

# Thirty tickets across §56's six categories, five each.
#
# A literal list rather than generated data, and no randomness: a demo whose numbers differ on
# every run cannot be described in a README, and a reviewer comparing two runs would see a
# difference and have to work out that it is not a bug.
#
#   (category, subject, description, priority, customer index, flow)
#
# `flow` is how far through the lifecycle the ticket has travelled, and it is spelled out
# because §5's transitions are strict — `open → assigned → in_progress → waiting_for_customer
# or resolved → closed` — and closing is its own endpoint. A seed that set `status` directly
# would produce a history the application would never have written, with no events to explain it.
TICKETS: list[tuple[str, str, str, TicketPriority, int, str]] = [
    # --- Billing ---------------------------------------------------------
    (
        "Billing",
        "Charged twice for the same order",
        "My card statement shows two charges of 84.00 GBP on the same day for order "
        "NW-40218. I only placed one order. Please refund the duplicate.",
        TicketPriority.URGENT,
        0,
        "resolved",
    ),
    (
        "Billing",
        "Invoice missing our VAT number",
        "Invoices for the last two months are missing our VAT number, so our finance team "
        "cannot reclaim the tax. Can you reissue them with GB 412 8876 21 on them?",
        TicketPriority.MEDIUM,
        1,
        "in_progress",
    ),
    (
        "Billing",
        "Wrong currency on my invoice",
        "I am billed in GBP but our account is in the Netherlands and we need EUR invoices "
        "for our bookkeeping.",
        TicketPriority.LOW,
        4,
        "waiting_for_customer",
    ),
    (
        "Billing",
        "Refund not received after three weeks",
        "I returned order NW-39744 on the 3rd and was told the refund was approved, but "
        "nothing has arrived. It has been three weeks.",
        TicketPriority.HIGH,
        2,
        "in_progress",
    ),
    (
        "Billing",
        "Payment method declined but order shipped",
        "My card was declined at checkout yet the order still shipped and I have been "
        "charged. I would like to understand what happened.",
        TicketPriority.MEDIUM,
        5,
        "closed",
    ),
    # --- Account ---------------------------------------------------------
    (
        "Account",
        "Cannot log in after email change",
        "I changed my email address last week and now the password reset link goes to the "
        "old address, which I no longer have access to.",
        TicketPriority.HIGH,
        3,
        "in_progress",
    ),
    (
        "Account",
        "Add a second administrator",
        "We would like our operations manager to have administrator rights so she can manage "
        "seats when I am away.",
        TicketPriority.MEDIUM,
        6,
        "resolved",
    ),
    (
        "Account",
        "Two-factor authentication codes rejected",
        "The authenticator codes are being rejected even though the phone's clock is set to "
        "automatic. I am locked out of the admin panel.",
        TicketPriority.URGENT,
        0,
        "in_progress",
    ),
    (
        "Account",
        "Remove a user who has left the company",
        "Please deactivate the account for our former colleague. I do not want to delete it "
        "in case we need the history.",
        TicketPriority.LOW,
        7,
        "closed",
    ),
    (
        "Account",
        "Merge two accounts into one",
        "We accidentally created two accounts during onboarding and would like the orders "
        "from the second moved into the first.",
        TicketPriority.MEDIUM,
        1,
        "assigned",
    ),
    # --- Technical -------------------------------------------------------
    (
        "Technical",
        "API returns 429 under normal load",
        "Since Monday our integration is getting rate limited during our normal nightly sync "
        "of about 4,000 records. It worked fine last week.",
        TicketPriority.URGENT,
        2,
        "in_progress",
    ),
    (
        "Technical",
        "Webhook not firing for status changes",
        "Our endpoint has not received a single ticket.status_changed event since the 14th. "
        "The endpoint is up; we tested it manually.",
        TicketPriority.HIGH,
        3,
        "waiting_for_customer",
    ),
    (
        "Technical",
        "Attachments fail to upload above 5 MB",
        "PDFs larger than about 5 MB fail with an error. Smaller ones work. Our contracts "
        "are usually 8 to 12 MB.",
        TicketPriority.HIGH,
        4,
        "resolved",
    ),
    (
        "Technical",
        "Search returns no results for accented names",
        "Searching for a customer whose surname is Lindqvist works, but Costa with an accent "
        "in the first name does not match.",
        TicketPriority.LOW,
        5,
        "assigned",
    ),
    (
        "Technical",
        "Dashboard totals do not match the ticket list",
        "The dashboard says 42 open tickets but the ticket list shows 39 when I filter by "
        "open. Which one is right?",
        TicketPriority.MEDIUM,
        6,
        "in_progress",
    ),
    # --- Subscription ----------------------------------------------------
    (
        "Subscription",
        "Upgrade to Growth mid-cycle",
        "We want to move from Starter to Growth today. How is the partial month billed, and "
        "when do the extra seats become available?",
        TicketPriority.MEDIUM,
        7,
        "resolved",
    ),
    (
        "Subscription",
        "Cancel at the end of the term",
        "Please confirm that our subscription will not renew on 1 March and that we keep "
        "access until then.",
        TicketPriority.LOW,
        0,
        "closed",
    ),
    (
        "Subscription",
        "Seat count wrong after downgrade",
        "We downgraded to Starter last month but still have 10 seats listed. The plan page says 3.",
        TicketPriority.HIGH,
        1,
        "in_progress",
    ),
    (
        "Subscription",
        "Trial ended without warning",
        "Our trial converted to a paid plan without any email beforehand. We would like the "
        "charge reversed; we had not decided yet.",
        TicketPriority.HIGH,
        2,
        "resolved",
    ),
    (
        "Subscription",
        "Annual billing discount",
        "Is there a discount for paying annually instead of monthly? We are planning our "
        "budget for next year.",
        TicketPriority.LOW,
        3,
        "assigned",
    ),
    # --- Policy ----------------------------------------------------------
    (
        "Policy",
        "Return outside the 30 day window",
        "The item arrived on 2 January and we opened it only this week because the office "
        "was closed. Are we still able to return it?",
        TicketPriority.MEDIUM,
        4,
        "waiting_for_customer",
    ),
    (
        "Policy",
        "Warranty on a replaced unit",
        "You replaced a faulty unit in October. Does the 24 month warranty restart from the "
        "replacement, or continue from the original purchase?",
        TicketPriority.MEDIUM,
        5,
        "resolved",
    ),
    (
        "Policy",
        "Can we ship to a freight forwarder",
        "We would like to ship to a forwarder in the Netherlands. Is that covered by your "
        "standard shipping and returns policy?",
        TicketPriority.LOW,
        6,
        "assigned",
    ),
    (
        "Policy",
        "Data retention after cancellation",
        "If we cancel, how long do you keep our ticket history, and can we export it before "
        "the account closes?",
        TicketPriority.HIGH,
        7,
        "in_progress",
    ),
    (
        "Policy",
        "GDPR request for a former employee",
        "A former employee has asked us to delete their personal data from our support "
        "history. What does that mean for tickets they raised?",
        TicketPriority.URGENT,
        0,
        "in_progress",
    ),
    # --- General ---------------------------------------------------------
    (
        "General",
        "Thank you to Priya",
        "Priya spent almost an hour on the phone with us last Thursday and got the migration "
        "working. Please pass on our thanks.",
        TicketPriority.LOW,
        1,
        "closed",
    ),
    (
        "General",
        "Opening hours over the holidays",
        "Will the support desk be open between Christmas and New Year? We are planning a "
        "release in that window.",
        TicketPriority.LOW,
        2,
        "resolved",
    ),
    (
        "General",
        "Request for a feature: bulk status change",
        "We regularly need to close a few hundred tickets after a release. A bulk action in "
        "the ticket list would save us a lot of clicking.",
        TicketPriority.LOW,
        3,
        "assigned",
    ),
    (
        "General",
        "Onboarding call for our new team",
        "Three new people joined our support team this month. Is there an onboarding session "
        "we can book?",
        TicketPriority.MEDIUM,
        4,
        "resolved",
    ),
    (
        "General",
        "Feedback on the new dashboard",
        "The new analytics page is much clearer than the old one. One suggestion: let us pin "
        "the date range so it survives a page reload.",
        TicketPriority.LOW,
        5,
        "assigned",
    ),
]

_AGENT_REPLY = (
    "Thanks for getting in touch — I have picked this up and I am looking into it now. "
    "I will come back to you with an update shortly."
)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. The script runs to the end even after a failure."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ok    {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}{f' -- {detail}' if detail else ''}")


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# --------------------------------------------------------------------------
# The seed itself
# --------------------------------------------------------------------------


async def seed(*, reset: bool) -> int:
    settings = get_settings()

    async with SessionFactory() as session:
        existing = await _find_organization(session)

    if existing is not None:
        if not reset:
            print(
                f"\n'{ORGANIZATION_NAME}' already exists (id {existing.id}).\n"
                "Re-run with --reset to delete it and seed again; nothing was changed."
            )
            return 2
        async with SessionFactory() as session:
            await _reset(session, existing)

    async with SessionFactory() as session:
        context = await _create_org_and_users(session)
        customers = await _create_customers(session, context)
        tickets = await _create_tickets(session, context, customers)
        await _backdate(session)
        documents = await _create_documents(session, context)

    section("AI analysis — queued by the product, completed by nobody")
    queued, produced = await _analysis_state(context)
    check(
        "every ticket is queued for analysis, twice",
        queued == 2 * len(tickets),
        f"{queued} of {2 * len(tickets)}",
    )
    check("not one result was invented (§60)", produced == 0, f"{produced} with a result")
    print(
        f"  {queued} rows are waiting — a classify and a sentiment per ticket, all pending.\n"
        "  A worker with AI_API_KEY configured fills them on its own; nothing here invents one."
    )

    await _verify(tickets, documents)

    section("Sign in with")
    print(f"  admin    {ADMIN[1]}   ({ADMIN[0]})")
    print(f"  manager  {MANAGER[1]}   ({MANAGER[0]})")
    for name, email in AGENTS:
        print(f"  agent    {email}   ({name})")
    print(f"\n  password: {PASSWORD}")

    if not settings.EMBEDDING_API_KEY:
        section("The knowledge base is not searchable yet, and this is why")
        print(
            "  The five documents were registered and queued, but ingestion needs an\n"
            "  embedding provider: neither Anthropic nor Groq publishes an embedding model,\n"
            "  and EMBEDDING_API_KEY is not set. With a worker running and a key configured,\n"
            "  they move from pending to completed and become searchable on their own."
        )

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


async def _find_organization(session: AsyncSession) -> Organization | None:
    # Annotated rather than returned directly: `AsyncSession.scalar` is typed as `Any` in
    # SQLAlchemy's stubs, and mypy's `no-any-return` is right to object to a bare `return` of it.
    found: Organization | None = await session.scalar(
        select(Organization).where(Organization.slug == SLUG)
    )
    return found


async def _reset(session: AsyncSession, organization: Organization) -> None:
    """Remove the demo tenant and everything it owns.

    **One statement, because the schema does the rest.** Every tenant-owned table declares
    `organization_id` with `ON DELETE CASCADE` (`app/models/base.py`), so deleting the
    organization row removes its users, customers, tickets, messages, events, attachments,
    notifications, audit rows, and knowledge chunks. Deleting them table by table here would be
    a second, hand-maintained copy of the cascade — and the copy that goes stale the first time
    a table is added.
    """
    section(f"Resetting — deleting '{ORGANIZATION_NAME}' and everything it owns")
    await session.execute(delete(Organization).where(Organization.id == organization.id))
    await session.commit()
    check("the organization and its data are gone", await _find_organization(session) is None)


async def _create_org_and_users(session: AsyncSession) -> TenantContext:
    section("Organization and staff")

    # `register` rather than a hand-built Organization: it writes the tenant, its administrator,
    # the default SLA policies, and the founding audit row in one transaction, and it is the
    # same path the registration route takes. A seeded tenant that skipped the SLA policies would
    # have inert timers and a demo that quietly proves nothing.
    result = await auth_service.register(
        session,
        RegisterRequest(
            organization_name=ORGANIZATION_NAME,
            name=ADMIN[0],
            email=ADMIN[1],
            password=PASSWORD,
        ),
    )
    admin = result.user
    check("the organization and its administrator exist", admin is not None)
    check("registration returned a session", bool(result.access_token))
    check("the registered tenant is the one we asked for", admin.email == ADMIN[1])
    check("the first account in a tenant is its administrator", admin.role is UserRole.ADMIN)

    context = TenantContext(
        user_id=admin.id,
        organization_id=admin.organization_id,
        role=admin.role,
        email=admin.email,
    )

    name, email = MANAGER
    await user_service.create_user(
        session,
        context,
        UserCreate(name=name, email=email, password=PASSWORD, role=UserRole.MANAGER),
    )
    for name, email in AGENTS:
        await user_service.create_user(
            session,
            context,
            UserCreate(name=name, email=email, password=PASSWORD, role=UserRole.AGENT),
        )

    staff = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.organization_id == context.organization_id)
    )
    # §56's named accounts: one admin, one manager, and the agents.
    expected = 2 + len(AGENTS)
    check(f"{expected} accounts exist, admin included", staff == expected, f"found {staff}")
    return context


async def _create_customers(session: AsyncSession, context: TenantContext) -> list[uuid.UUID]:
    section("Customers")

    ids: list[uuid.UUID] = []
    for name, email, phone in CUSTOMERS:
        customer = await customer_service.create_customer(
            session,
            context,
            CustomerCreate(name=name, email=email, phone=phone),
        )
        ids.append(customer.id)

    check(f"{len(CUSTOMERS)} customers created", len(ids) == len(CUSTOMERS))
    return ids


async def _create_tickets(
    session: AsyncSession, context: TenantContext, customers: list[uuid.UUID]
) -> list[uuid.UUID]:
    section(f"Tickets — {len(TICKETS)} across six categories")

    agents = await _agents(session, context)
    check("at least one agent is available to assign", bool(agents))

    created: list[uuid.UUID] = []
    failures: list[str] = []

    for index, row in enumerate(TICKETS):
        category, subject, description, priority, customer_index, flow = row
        agent = agents[index % len(agents)] if agents else None

        try:
            ticket = await ticket_service.create_ticket(
                session,
                context,
                TicketCreate(
                    subject=subject,
                    description=description,
                    customer_id=customers[customer_index],
                    category=category,
                    priority=priority,
                ),
            )
            if agent is not None:
                await ticket_service.assign_ticket(
                    session, context, ticket.id, assigned_agent_id=agent.id
                )
                await _walk_flow(
                    session, context, ticket.id, flow, agent_responds=flow != "assigned"
                )
            created.append(ticket.id)
        except AppError as exc:
            # Reported and skipped rather than raised: one ticket that cannot complete a
            # transition is worth knowing about, and is not a reason to abandon the other 29.
            failures.append(f"{subject}: {exc.message}")

    check(f"{len(TICKETS)} tickets created", len(created) == len(TICKETS), f"{len(created)}")
    check("every ticket reached its intended state", not failures, "; ".join(failures[:3]))
    return created


async def _walk_flow(
    session: AsyncSession,
    context: TenantContext,
    ticket_id: uuid.UUID,
    flow: str,
    *,
    agent_responds: bool,
) -> None:
    """Advance one ticket along §5's permitted transitions.

    Each step is a real service call, so it writes a `ticket_events` row and an audit row and
    stages the notifications the API would stage. Setting `status` directly would produce the
    same final value with none of the history — and the history is what the timeline and the SLA
    analytics are computed from.

    The steps are per endpoint rather than per status because the endpoints are: `change_status`
    owns only the four middle edges, and closing a resolved ticket is `POST /close`, a separate
    act — §3's "confirm resolution" belongs to the customer, not to the agent who decided it was
    fixed.
    """
    if agent_responds:
        # The first public agent reply is what sets `first_response_at`, which every
        # response-time number in the analytics is derived from. A demo with no replies has a
        # response-time report of zeroes.
        await message_service.post_reply(
            session, context, ticket_id, MessageCreate(body=_AGENT_REPLY)
        )

    if flow == "assigned":
        return

    await ticket_service.change_status(
        session, context, ticket_id, TicketStatusUpdate(status=TicketStatus.IN_PROGRESS)
    )
    if flow == "in_progress":
        return

    if flow == "waiting_for_customer":
        await ticket_service.change_status(
            session,
            context,
            ticket_id,
            TicketStatusUpdate(status=TicketStatus.WAITING_FOR_CUSTOMER),
        )
        return

    await ticket_service.change_status(
        session, context, ticket_id, TicketStatusUpdate(status=TicketStatus.RESOLVED)
    )
    if flow == "resolved":
        return

    await ticket_service.close_ticket(session, context, ticket_id)


async def _backdate(session: AsyncSession) -> None:
    """Spread the seeded tickets across the last 45 days.

    **The one place this script writes outside the service layer, and it is not laziness.** A
    ticket's `created_at` is set by the database at insert; no service call accepts a timestamp,
    because no real client gets to choose when its request happened. So a seed that used only the
    service layer produces thirty tickets all created in the same second — and every time-series
    panel in the product (ticket volume, response times, resolution times, SLA compliance)
    renders a single spike on today's date.

    The shift is derived once per ticket and applied to every timestamp that ticket owns, so a
    ticket created 40 days ago with a first response four hours later stays four hours later.
    Moving `created_at` alone would be worse than not backdating at all: it would produce tickets
    whose first response preceded their creation.
    """
    section("Backdating — spreading the demo across the last 45 days")

    rows = (
        await session.execute(
            select(
                Ticket.id,
                Ticket.created_at,
                Ticket.first_response_at,
                Ticket.resolved_at,
                Ticket.closed_at,
            ).order_by(Ticket.id)
        )
    ).all()
    if not rows:
        check("there is something to backdate", False)
        return

    now = datetime.now(UTC)
    # 45 days spread evenly, so the newest ticket is a few hours old and the oldest is about six
    # weeks back — enough that a 30-day analytics window has data on both sides of its boundary.
    span = timedelta(days=45)
    step = span / len(rows)

    deltas: dict[uuid.UUID, timedelta] = {}
    ticket_values: list[dict[str, object]] = []

    for position, (ticket_id, created_at, first_response, resolved, closed) in enumerate(rows):
        target = now - span + step * position
        delta = target - created_at
        deltas[ticket_id] = delta
        ticket_values.append(
            {
                "id": ticket_id,
                "created_at": target,
                # A timestamp that is absent means the thing it records never happened, so
                # absent stays absent — and `None + delta` would raise anyway.
                "first_response_at": None if first_response is None else first_response + delta,
                "resolved_at": None if resolved is None else resolved + delta,
                "closed_at": None if closed is None else closed + delta,
            }
        )

    await _bulk_update(session, Ticket, ticket_values)
    await _shift_children(session, Message, deltas)
    await _shift_children(session, TicketEvent, deltas)
    await session.commit()

    oldest, newest = (
        await session.execute(select(func.min(Ticket.created_at), func.max(Ticket.created_at)))
    ).one()
    check("the newest ticket is recent", newest is not None and (now - newest) < timedelta(days=2))
    check(
        "the oldest ticket is weeks back",
        oldest is not None and (now - oldest) > timedelta(days=30),
    )


async def _bulk_update(
    session: AsyncSession, model: type[Any], values: list[dict[str, object]]
) -> None:
    """ORM bulk UPDATE by primary key — one statement for the whole set of rows.

    A list of parameter dictionaries is the supported spelling, and it is what the driver's
    `executemany` is for; looping one `UPDATE` per row would be thirty round trips to say the
    same thing.
    """
    if values:
        await session.execute(update(model), values)


async def _shift_children(
    session: AsyncSession, model: type[Any], deltas: dict[uuid.UUID, timedelta]
) -> None:
    """Move every child row's `created_at` by its parent ticket's delta.

    Messages and events are part of the ticket's history — the conversation thread and the event
    list both render in timestamp order — so a ticket created 40 days ago whose replies were all
    created today would render as a conversation that happened after the ticket was resolved.
    """
    rows = (await session.execute(select(model.id, model.ticket_id, model.created_at))).all()
    values = [
        {"id": row_id, "created_at": created_at + deltas[ticket_id]}
        for row_id, ticket_id, created_at in rows
        if ticket_id in deltas
    ]
    await _bulk_update(session, model, values)


async def _create_documents(session: AsyncSession, context: TenantContext) -> list[uuid.UUID]:
    section(f"Knowledge documents — {len(DOCUMENTS)}")

    ids: list[uuid.UUID] = []
    for title, content in DOCUMENTS:
        document = await knowledge_service.create_manual(
            session,
            context,
            KnowledgeDocumentCreate(title=title, content=content),
        )
        ids.append(document.id)

    unpublished = await session.scalar(
        select(func.count())
        .select_from(KnowledgeDocument)
        .where(
            KnowledgeDocument.organization_id == context.organization_id,
            KnowledgeDocument.is_published.is_(False),
        )
    )
    check(f"{len(DOCUMENTS)} documents registered", len(ids) == len(DOCUMENTS))
    check(
        "they are queued for ingestion, not yet published",
        unpublished == len(DOCUMENTS),
        f"{unpublished}",
    )
    return ids


async def _analysis_state(context: TenantContext) -> tuple[int, int]:
    """How many analyses are queued, and how many have produced a result.

    Read back from the database rather than assumed, because the interesting number is the
    second one: a queue with rows in it and no results is the correct outcome here, and a queue
    with results would mean something had invented them.
    """
    async with SessionFactory() as session:
        queued: int | None = await session.scalar(
            select(func.count())
            .select_from(AIAnalysis)
            .where(AIAnalysis.organization_id == context.organization_id)
        )
        produced: int | None = await session.scalar(
            select(func.count())
            .select_from(AIAnalysis)
            .where(
                AIAnalysis.organization_id == context.organization_id,
                AIAnalysis.result.is_not(None),
            )
        )
    return queued or 0, produced or 0


async def _verify(tickets: list[uuid.UUID], documents: list[uuid.UUID]) -> None:
    """Count what is actually in the database rather than trusting this script's own summary.

    A seed that reported success by counting its own loop iterations would report the same
    success if every write had been rolled back underneath it.
    """
    section("Verification — read back from the database")

    async with SessionFactory() as session:
        ticket_count = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.id.in_(tickets))
        )
        document_count = await session.scalar(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(KnowledgeDocument.id.in_(documents))
        )
        message_count = await session.scalar(
            select(func.count()).select_from(Message).where(Message.ticket_id.in_(tickets))
        )
        by_status = (
            await session.execute(
                select(Ticket.status, func.count())
                .where(Ticket.id.in_(tickets))
                .group_by(Ticket.status)
                .order_by(func.count().desc())
            )
        ).all()
        by_category = (
            await session.execute(
                select(Ticket.category, func.count())
                .where(Ticket.id.in_(tickets))
                .group_by(Ticket.category)
                .order_by(Ticket.category)
            )
        ).all()

    check(
        f"{len(TICKETS)} tickets in the database",
        ticket_count == len(TICKETS),
        f"{ticket_count}",
    )
    check(f"{len(DOCUMENTS)} documents in the database", document_count == len(DOCUMENTS))
    check("tickets carry conversations", (message_count or 0) > 0, f"{message_count} messages")
    check("tickets are spread across several statuses", len(by_status) >= 3, str(by_status))
    check("tickets cover all six categories", len(by_category) == 6, str(by_category))

    print("\n  tickets by status:")
    for status_value, count in by_status:
        print(f"    {status_value.value:<22} {count}")
    print("  tickets by category:")
    for category, count in by_category:
        print(f"    {category:<22} {count}")


async def _agents(session: AsyncSession, context: TenantContext) -> list[User]:
    result = await session.execute(
        select(User)
        .where(
            User.organization_id == context.organization_id,
            User.role == UserRole.AGENT,
        )
        .order_by(User.email)
    )
    return list(result.scalars().all())


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Acme Support demo tenant.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help=f"Delete an existing '{ORGANIZATION_NAME}' organization and seed again.",
    )
    args = parser.parse_args()

    print(f"Seeding '{ORGANIZATION_NAME}'...")
    code = run(seed(reset=args.reset))
    sys.exit(code)


if __name__ == "__main__":
    main()
