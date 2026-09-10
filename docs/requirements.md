# SupportFlow — Product Requirements

> Phase A deliverable. Derived from `SupportFlow_Master_Build_Specification.txt`.
> This document defines *what* the system must do. Architecture (*how*) lives in
> [architecture.md](architecture.md); deviations from the spec are recorded in
> [architecture-decisions.md](architecture-decisions.md).

## 1. Product summary

SupportFlow is a multi-tenant customer support and helpdesk platform. Support teams
manage customer tickets through a defined lifecycle, assisted — never replaced — by
AI for classification, sentiment, summarization, and draft replies. Each organization
maintains a private knowledge base that grounds AI answers in its own documented
policies rather than model invention.

The unit of tenancy is the **organization**. Every business record belongs to exactly
one organization, and no request may ever read or write across that boundary.

## 2. Actors

| Actor | Description |
|---|---|
| **Admin** | Owns the organization. Manages users, roles, SLA policy, knowledge base, AI settings. Sees everything within the org. |
| **Manager** | Oversees support delivery. Sees all org tickets, assigns agents, adjusts priority, reads analytics and SLA risk. |
| **Agent** | Works tickets. Sees tickets assigned to them, replies to customers, writes internal notes, requests AI help. |
| **Customer** | External end user. Creates tickets, sees and replies to **only their own** tickets. |

Admin, Manager, and Agent are internal staff of an organization. Customer is an
external party linked to an organization through a `Customer` record.

## 3. Permission matrix

`✓` = permitted · `—` = denied · `own` = restricted to records the actor owns ·
`assigned` = restricted to tickets assigned to that agent

| Capability | Admin | Manager | Agent | Customer |
|---|:--:|:--:|:--:|:--:|
| **Organization** | | | | |
| View organization settings | ✓ | ✓ | — | — |
| Update organization settings | ✓ | — | — | — |
| **Users** | | | | |
| List users | ✓ | ✓ | — | — |
| Create user | ✓ | — | — | — |
| Update user role | ✓ | — | — | — |
| Deactivate user | ✓ | — | — | — |
| View own profile | ✓ | ✓ | ✓ | ✓ |
| **Customers** | | | | |
| List / search customers | ✓ | ✓ | ✓ | — |
| Create customer | ✓ | ✓ | ✓ | — |
| Update customer | ✓ | ✓ | ✓ | — |
| **Tickets** | | | | |
| List all org tickets | ✓ | ✓ | — | — |
| List assigned tickets | ✓ | ✓ | ✓ | — |
| List own tickets | — | — | — | ✓ own |
| Create ticket | ✓ | ✓ | ✓ | ✓ own |
| View ticket detail | ✓ | ✓ | ✓ assigned | ✓ own |
| Assign / reassign agent | ✓ | ✓ | — | — |
| Change priority | ✓ | ✓ | — | — |
| Change status | ✓ | ✓ | ✓ assigned | — |
| Confirm resolution / close own ticket | ✓ | ✓ | ✓ assigned | ✓ own |
| Reopen ticket | ✓ | ✓ | ✓ assigned | ✓ own |
| **Messages** | | | | |
| Read public conversation | ✓ | ✓ | ✓ assigned | ✓ own |
| Read internal notes | ✓ | ✓ | ✓ assigned | — |
| Post customer-facing reply | ✓ | ✓ | ✓ assigned | ✓ own |
| Post internal note | ✓ | ✓ | ✓ assigned | — |
| **Attachments** | | | | |
| Upload to ticket | ✓ | ✓ | ✓ assigned | ✓ own |
| Download from ticket | ✓ | ✓ | ✓ assigned | ✓ own |
| **AI** | | | | |
| Request analysis / summary | ✓ | ✓ | ✓ assigned | — |
| Request suggested reply | ✓ | ✓ | ✓ assigned | — |
| Query knowledge base | ✓ | ✓ | ✓ | — |
| Configure AI settings | ✓ | — | — | — |
| View AI usage / cost | ✓ | ✓ | — | — |
| **Knowledge base** | | | | |
| List / read documents | ✓ | ✓ | ✓ | — |
| Upload document | ✓ | — | — | — |
| Delete document | ✓ | — | — | — |
| **SLA** | | | | |
| View SLA status | ✓ | ✓ | ✓ assigned | — |
| Configure SLA policy | ✓ | — | — | — |
| **Analytics** | | | | |
| Org-wide analytics | ✓ | ✓ | — | — |
| Own performance only | ✓ | ✓ | ✓ own | — |
| **Audit log** | | | | |
| View audit log | ✓ | — | — | — |
| **Notifications** | | | | |
| List / mark own read | ✓ | ✓ | ✓ | ✓ |

Two rules override every row above:

1. **Tenant scope.** Access is only ever evaluated *within* the caller's organization.
   A row marked `✓` never grants access to another organization's data.
2. **Server-side enforcement.** The matrix is enforced in the API layer. Frontend
   route guards are a usability affordance, never a security control.

## 4. Core workflows

### 4.1 Customer raises a ticket

1. Customer submits subject, description, optional attachments.
2. API validates input, resolves tenant from the authenticated session, persists the
   ticket as `OPEN`, and writes a `TICKET_CREATED` event and audit record.
3. API enqueues AI analysis and returns immediately — it does not wait for the LLM.
4. Worker classifies category/subcategory, scores sentiment, recommends priority, and
   persists an `AI_ANALYSIS` row plus AI-derived fields on the ticket.
5. Business rules compute *effective* priority from the AI recommendation plus
   overrides (VIP customer, SLA risk, known outage).
6. Worker publishes an event; connected staff clients update without refresh.

### 4.2 Agent works a ticket

1. Agent opens ticket detail: conversation, customer context, SLA countdown, AI
   summary, AI sentiment.
2. Agent optionally requests a suggested reply. The system retrieves relevant
   knowledge chunks for the org, builds a grounded prompt, and returns a draft
   labelled as AI-generated.
3. Agent edits or discards the draft, then **manually** sends. The system records
   whether the draft was accepted, edited, or regenerated.
4. Status transitions validate against the lifecycle and emit events, audit records,
   and notifications.

### 4.3 Knowledge ingestion

Admin uploads a document → stored in object storage with metadata in Postgres →
worker extracts text, cleans, chunks, and embeds → chunks and vectors persisted →
document becomes searchable. Status is observable throughout (`pending` →
`processing` → `ready` / `failed`).

### 4.4 Knowledge question (RAG)

Question → embedding → org-scoped vector similarity search → top chunks → grounded
prompt → answer plus source references. If no chunk clears the relevance threshold,
the system states that the knowledge base lacks the information rather than
answering from model priors. Citations are never fabricated.

### 4.5 SLA monitoring

Each priority carries configurable first-response and resolution targets. A periodic
job finds tickets approaching or breaching a deadline and notifies the assigned agent
and managers.

## 5. Ticket lifecycle

States: `OPEN`, `ASSIGNED`, `IN_PROGRESS`, `WAITING_FOR_CUSTOMER`, `RESOLVED`, `CLOSED`.

Permitted transitions:

| From | To |
|---|---|
| `OPEN` | `ASSIGNED` |
| `ASSIGNED` | `IN_PROGRESS` |
| `IN_PROGRESS` | `WAITING_FOR_CUSTOMER`, `RESOLVED` |
| `WAITING_FOR_CUSTOMER` | `IN_PROGRESS` |
| `RESOLVED` | `CLOSED` |
| `CLOSED` | `OPEN` (explicit reopen action only) |

Any other transition is rejected with `INVALID_TICKET_TRANSITION`. Status is never
mutated by a blind field update; transitions go through an action that validates the
edge, records a ticket event, and audits where appropriate.

## 6. Priority

Values: `LOW`, `MEDIUM`, `HIGH`, `URGENT`.

AI-recommended priority and final business priority are stored separately. Business
rules may raise the effective priority above the AI recommendation; the recommendation
is retained for comparison and for demonstrating AI accuracy over time.

## 7. Non-functional requirements

**Security.** Passwords hashed with a memory-hard algorithm. Short-lived access
tokens; revocable refresh tokens. RBAC and tenant isolation enforced server-side on
every protected resource. All input validated by Pydantic. ORM-parameterized queries
only. Upload validation on size, declared type, sniffed type, and extension. No
secrets in source control. No tokens, passwords, or unnecessary customer content in
logs. AI output treated as untrusted until validated.

**Reliability.** Long-running work runs in background jobs with bounded retries and
explicit failure states. AI provider failure degrades gracefully — the ticket still
exists and stays workable. Health and readiness endpoints cover Postgres and Redis.

**Performance.** All list endpoints paginated; no unbounded ticket or message query.
Indexes on `organization_id`, ticket status, priority, assigned agent, customer, and
`created_at`. Expensive analytics cached in Redis with deliberate invalidation.
N+1 queries treated as defects.

**Observability.** Structured logs carrying a request ID, status code, and duration.
Background task lifecycle, AI failures, database failures, and authentication
failures all logged.

**Scalability.** API instances scale horizontally. No critical state in local process
memory. WebSocket fan-out designed for multi-instance deployment from the start.

**Maintainability.** Modular boundaries without needless microservices. AI vendor
swappable behind an abstraction. Schema changes only through migrations.

## 8. Success criteria

The project is complete when all of the following hold:

1. The backend runs and is fully exercisable without the frontend.
2. Auth works: register, login, refresh, logout, inactive-user rejection.
3. RBAC is enforced server-side and centrally, matching §3.
4. Cross-tenant access is proven impossible by automated tests covering tickets,
   customers, knowledge, audit logs, and WebSocket subscriptions.
5. Ticket and message flows work end-to-end, including transition validation.
6. Redis genuinely serves caching, rate limiting, and pub/sub.
7. Celery tasks execute, retry on transient failure, and surface terminal failures.
8. WebSockets deliver real-time updates to authorized clients only.
9. AI features work against a real provider, with validated structured output.
10. RAG retrieves only org-owned documents and cites real sources.
11. Analytics come from real aggregation queries, never hardcoded values.
12. Attachments upload and download securely through object storage.
13. Audit logs capture the actions listed in the spec.
14. Migrations apply cleanly to an empty database.
15. Tests pass; `docker compose up` works; CI is green.
16. README explains architecture and security posture; seed data makes the app
    demonstrable; no secrets are committed.
