# SupportFlow — Architecture

> Phase B deliverable. Requirements live in [requirements.md](requirements.md);
> deviations from the build specification are recorded in
> [architecture-decisions.md](architecture-decisions.md).

## 1. System overview

```
                        ┌──────────────────────┐
                        │   React SPA (Vite)   │
                        │  TS · Tailwind · RQ  │
                        └──────┬────────┬──────┘
                        HTTPS  │        │  WSS
                               ▼        ▼
                        ┌──────────────────────┐
                        │   FastAPI (uvicorn)  │
                        │  api · services ·    │
                        │  repositories · ws   │
                        └──┬────────┬───────┬──┘
                           │        │       │
              ┌────────────▼──┐  ┌──▼────┐  └──────────┐
              │  PostgreSQL   │  │ Redis │             ▼
              │  17 + pgvector│  │       │      ┌─────────────┐
              └───────▲───────┘  └──▲─┬──┘      │ S3-compatible│
                      │             │ │         │   storage    │
                      │      broker │ │ pub/sub └──────▲───────┘
                      │             │ │                │
                   ┌──┴─────────────┴─▼────────────────┴──┐
                   │        Celery workers + beat         │
                   │  ai · knowledge · sla · email        │
                   └──────────────────┬───────────────────┘
                                      ▼
                              ┌───────────────┐
                              │  AI provider  │
                              │ LLM + embed   │
                              └───────────────┘
```

Redis carries a bidirectional arrow because it serves two distinct roles: Celery
broker/result backend, and pub/sub transport for real-time events. Workers publish;
API instances subscribe and fan out to their own WebSocket clients.

## 2. Component responsibilities

| Component | Responsibility |
|---|---|
| **React SPA** | Presentation and role-specific navigation. Holds no security decisions. |
| **FastAPI** | HTTP surface, authn/authz, validation, tenant scoping, orchestration, WebSocket endpoints. |
| **Services** | Business rules: ticket lifecycle, priority policy, SLA math, audit writes. Tenant-aware, transport-agnostic. |
| **Repositories** | Data access. Every query is tenant-scoped by construction. |
| **PostgreSQL** | System of record; relational integrity; full-text search; vector search via pgvector. |
| **Redis** | Cache, rate-limit counters, Celery broker/backend, pub/sub. |
| **Celery workers** | AI analysis, embedding, document ingestion, email, report generation. |
| **Celery beat** | Periodic SLA sweeps and scheduled maintenance. |
| **AI service layer** | Provider-agnostic interface, prompt construction, structured-output validation, usage accounting. |
| **Object storage** | Attachment and source-document bytes. Never public; access mediated by the API. |

## 3. Backend layering

```
api/          HTTP concerns only — routing, status codes, dependency wiring
  ↓
services/     business rules, orchestration, transaction boundaries
  ↓
repositories/ tenant-scoped data access
  ↓
models/       SQLAlchemy ORM
```

Dependencies point downward only. A route never issues a query directly; a repository
never contains business rules. Workers reuse the same service layer, so background
and synchronous paths share one implementation of the rules.

## 4. Authentication flow

```
POST /auth/login  (email, password)
   │
   ├─ look up user by email  ── not found ──▶ 401 (generic message)
   ├─ verify Argon2id hash   ── mismatch ───▶ 401 (generic message)
   ├─ check is_active        ── inactive ───▶ 403
   │
   ├─ access token   JWT, short TTL, in-memory on the client
   └─ refresh token  opaque random, hashed at rest, HttpOnly cookie
```

The access token is a signed JWT carrying subject, organization, role, and expiry.
It is never persisted server-side; expiry is the revocation mechanism, which is why
its TTL is short.

The refresh token is **not** a JWT. It is high-entropy random material stored as a
hash in Postgres, which makes it individually revocable — a property JWTs cannot
offer without a blocklist. It rotates on every use, and reuse of a consumed token
revokes the whole family as a theft signal.

Because the refresh cookie is sent cross-origin, CORS runs with credentials against
an explicit origin allowlist, the cookie is scoped narrowly to the refresh path, and
the refresh endpoint carries CSRF protection. Login and refresh are rate-limited.

Authenticated request path:

```
Request + Bearer token
   → verify signature and expiry
   → load user, confirm still active
   → build TenantContext { user_id, organization_id, role }
   → route dependency asserts required permission
   → service receives the context and cannot query outside it
```

## 5. Multi-tenancy

The most security-critical property in the system. `organization_id` is derived from
the verified token and **never** read from a query parameter, path segment, or request
body. Any client-supplied organization identifier is ignored outright.

Defence in depth, three layers:

1. **Context.** Tenant identity is only obtainable from an authenticated principal.
2. **Repository.** Tenant-scoped repositories require a `TenantContext` at
   construction and inject the `organization_id` predicate into every query. Writing a
   tenant-scoped query without the filter requires bypassing the repository, which
   review and tests target.
3. **Schema.** Every tenant-owned table carries `organization_id` with a foreign key
   and an index; composite uniqueness is scoped per organization.

Cross-tenant reads return `404`, not `403` — a `403` would confirm that the record
exists in another organization. Tests assert this for tickets, customers, knowledge,
audit logs, and WebSocket subscriptions.

The schema half of this is specified in [data-model.md](data-model.md): the ERD, the
per-table column reference, and the constraints that make tenant isolation and the
AI-review rules structural rather than conventional.

## 6. AI flow

```
trigger (ticket created, or agent request)
   → enqueue task, return immediately
   → worker loads ticket within tenant scope
   → build prompt from a versioned template
   → provider call with timeout and bounded retry
   → parse into a Pydantic model  ── invalid ──▶ mark failed, do not persist
   → persist AI_ANALYSIS + AI_USAGE
   → update ticket AI fields
   → publish event → clients update live
```

Three invariants:

- **Provider-agnostic.** Application code depends on an `AIProvider` interface
  (`classify_ticket`, `analyze_sentiment`, `summarize_conversation`,
  `generate_response`, `generate_embedding`), never on a vendor SDK.
- **Output is untrusted.** Every response is parsed into a Pydantic schema before it
  touches the database. Malformed output is a handled failure, not an exception path.
- **Never autonomous.** AI drafts replies; a human sends them. No AI output reaches a
  customer without explicit human action.

Failure is contained: if the provider is down, ticket creation still succeeds and the
analysis carries a `failed` status that can be retried.

## 7. RAG flow

**Ingestion** (asynchronous, per document):

```
upload → validate → object storage → metadata row (status=pending)
   → worker: extract text → clean → chunk with overlap
   → embed each chunk → persist chunks + vectors → status=ready
```

**Query:**

```
question → embed → vector search WHERE organization_id = ctx.organization_id
   → top-k chunks above a relevance threshold
   │
   ├─ nothing clears threshold → "knowledge base lacks this information"
   └─ chunks found → grounded prompt → answer + real source references
```

Vector search uses pgvector with an HNSW index under cosine distance. The tenant
predicate is part of the query, so retrieval cannot cross organizations — the same
isolation guarantee as every other read, applied to the vector path.

Grounding rules: answer only from retrieved context, state gaps explicitly, never
invent policy, never fabricate a citation. Sources shown in the UI resolve to real
stored chunks.

## 8. Real-time flow

```
Agent A resolves a ticket
   → API commits the transaction
   → publish to Redis channel  org:{organization_id}
   → every API instance subscribed receives it
   → each fans out to its local WebSocket connections for that org
   → Agent B's UI updates without refresh
```

Connections authenticate before joining, and subscription is bound to the
organization in the verified token — a client cannot subscribe to another
organization's channel. Because fan-out goes through Redis rather than process
memory, any API instance can serve any client, which keeps horizontal scaling intact.

Publication happens after commit, so clients are never notified of a change that
later rolls back.

## 9. Background job flow

```
API enqueues (Redis broker) → worker executes → status recorded → event published
```

Transient failures (provider timeout, rate limit) retry with exponential backoff and
a bounded ceiling. Permanent failures (malformed output, missing record) fail fast to
a terminal state rather than burning retries. Tasks are written to tolerate
re-delivery: a duplicate analysis updates the existing row instead of creating a
second one.

Beat owns periodic work — chiefly the SLA sweep that finds tickets nearing a
first-response or resolution deadline and notifies the relevant staff.

## 10. Caching

Redis caches expensive read-mostly analytics aggregates under tenant-prefixed keys,
with TTLs plus explicit invalidation when underlying ticket data changes. Cheap
queries are not cached — a cache with no measured cost behind it adds staleness risk
and buys nothing.

## 11. Deployment shape

Local development runs infrastructure (Postgres, Redis, MinIO, Mailpit) in Docker
while the API, worker, and frontend run natively for fast reloads. Full-stack Compose
files exist for parity checks and CI.

Production runs the same container images behind a load balancer: multiple API
instances, separate worker and beat processes, managed Postgres and Redis, and
object storage. Migrations run as a discrete step before the new backend rolls out.
The target platform is deliberately undecided; nothing in the architecture depends on
a specific cloud.
