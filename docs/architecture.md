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

The lower half of this section is implementation, and the flow has one operation left to gain. **Phase
T (ADR-027)** built the provider abstraction, the call path, and the ledger. **Phase U (ADR-029)** built
§18's flow around them for the first two operations — classification and sentiment — with the route, the
queue, and the worker. **Phase V (ADR-030)** added §20's summary on the same worker and a rule that
decides when it does not need to run at all. Suggested replies (§21) are Phase W, and the flow gains its
last line when they land.

```
trigger (ticket created, or agent request)          ── ✅ U; ✅ V by request, when the conversation moved
   → enqueue task, return immediately                    ── ✅ U, on the `ai` queue
   → worker loads ticket within tenant scope             ── ✅ U (`ai_repository.py`, tenant in the WHERE)
   → build prompt from a versioned template              ── mechanism in T (`app/ai/prompts.py`); classification and sentiment text in U; summarization text in V; replies in W
   → provider call with timeout and bounded retry        ── ✅ T
   → parse into a Pydantic model  ── invalid ──▶ handled failure, no result  ✅ T
   → persist AI_USAGE (one row per attempt)              ── ✅ T; ✅ V records a served-from-storage answer as a row with `was_cached`
   → persist AI_ANALYSIS                                 ── ✅ U
   → update ticket AI fields                             ── ✅ U (never `tickets.priority`); a summary writes no field at all
   → publish event → clients update live                 ── ✅ U
```

**What Phase U added on top of T.** Two routes under the ticket that owns them (ADR-029 Decision 6),
`WorkerContext` — a tenant with no authority, so a task cannot authorize anything by construction
(Decision 1) — and `ai_analysis_service.py`, which owns the `ai_analyses` rows: they are written
`pending` at request time so the queue is visible, the task flips them to `processing` before it calls
anything, and a redelivered task finds them terminal and does nothing. Both operations run in **one**
task, so the ticket gets one timeline entry, one notification, and one announcement, because §18's
last three steps are singular; a failure inside the run is still contained to its own operation.

**What Phase V added on top of U.** §20's summary as a **second entry point over the same worker**
rather than a third member of `ANALYSIS_OPERATIONS` (ADR-030 Decision 5) — the operation differs, the
machinery does not. The entry point is where the freshness rule lives, because it is the only place
that can know a call is unnecessary: the newest `completed` summary's timestamp against the
conversation's newest message, both read from the database's clock, and one rule — a person spoke or
nobody did. `ai_service.record_cache_hit` writes the ledger row for the call that did not happen, so
the saving is a count of rows rather than an estimate, and `/analytics/overview` gained `cached_calls`
to read it. The flow above changes in exactly two places: the trigger can now be a request against a
ticket that is already analyzed, and one of §18's steps writes nothing — a summary stores a result and
touches no ticket field, which is why a summary-only run announces without notifying.

**What runs today.** `app/services/ai_service.py` is the single call path — `classify_ticket`,
`analyze_sentiment`, `summarize_conversation`, `generate_response` — and every one of them reduces
to a private `_run`: select the provider, call it with the client's timeout, retry a *transient*
failure up to `AI_MAX_ATTEMPTS` with jittered backoff, validate the answer into its schema, and
stage one `ai_usage` row for every attempt including the failed ones. The provider is chosen by
`AI_PROVIDER`, and **each vendor gets exactly one module, which is the only thing in the project that
imports its client** — `app/ai/claude.py` for the `anthropic` SDK and `app/ai/groq.py` for `httpx`.
Two real vendors behind one interface is what makes the boundary checkable rather than asserted: the
second one cost a module and changed nothing above it (ADR-028).

Three invariants, and how each is held:

- **Provider-agnostic.** Application code depends on the `AIProvider` protocol
  (`classify_ticket`, `analyze_sentiment`, `summarize_conversation`, `generate_response`) and never
  on a vendor SDK. `generate_embedding` is deliberately **not** in it yet: ADR-008 puts embeddings
  behind the same interface with a *different* vendor, and declaring the method now would give
  `ClaudeProvider` a stub it can never serve. It arrives in Phase X with the provider that can
  answer it.
- **Output is untrusted.** `app/ai/provider.py`'s `validate_output` is the one place a provider's
  payload is parsed, and it is called by both the real provider and the fake — so a model that
  answered in prose, truncated at `max_tokens`, invented an enum member, or returned a confidence of
  `1.4` produces an `AIOutputError` and no value. §18's requirement is structural rather than
  conventional: there is no path from a provider response to application code that skips it.
- **Never autonomous.** `SuggestedReply` has a body and no confidence, no status, and no sender —
  the schema cannot express "sent". §21's rule is enforced by what the types can say.

Customer-written text is fenced before it reaches a model, in `app/ai/prompts.py`: `as_untrusted`
wraps it in a delimited block under a sentence saying it is content rather than instruction, and
defuses any fence marker appearing inside it so the boundary cannot be spelled by the customer.
This is mitigation and not a guarantee — prompt injection is unsolved — which is why the two
invariants above are the containment that matters.

**Cost is recorded, not estimated.** Every attempt writes an `ai_usage` row with the provider's own
token counts, a `cost_usd` computed at write time from the published rate in `app/ai/pricing.py`,
the latency, and whether the attempt succeeded. A failed call is recorded rather than dropped,
because it consumed quota and may have been billed. **So is the call that was never made:** since
Phase V a served-from-storage answer writes a row with `was_cached` set and zeroes throughout, because
an absent row and a call that cost nothing look identical on a dashboard and only one of them is
evidence. `GET /analytics/overview` reads that table through the Phase S aggregate and reports `calls`,
`failed_calls`, and `cached_calls` as three overlapping counts over the rows that exist — the last two
each a subset of the first, so the number still answers "how many calls were made".

**Two obligations travel with the ledger, and both belong to the caller.** `ai_service` stages rows
and never commits, so whoever calls it commits — including after a failure, since a ledger that
rolled back with a failed request would be missing exactly when it matters. And because
`/analytics/overview` is cached while an `ai_usage` write does not bump the version integer, whoever
commits also owns the `cache.invalidate` — the same pattern `ticket_service` follows after its own
commit. Phase U's route is where both obligations land.

Failure is contained: a provider that is down, throttled, or holding a bad key raises
`AIServiceError` (503) and nothing else escapes, so a caller can treat every AI failure as one
condition. Nothing on a request path is blocked by it, because nothing on a request path calls it
yet.

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

Implemented in Phase R (ADR-025). This section was written in Phase B as a plan; it is now a
description of what runs.

```
A committed change
   → the service publishes after session.commit()
   → app/websocket/manager.py pipelines the ticket event and one envelope per notification
   → to Redis channel  org:{organization_id}
   → every API instance's subscriber task receives it
   → app/websocket/events.py decides, per local connection, whether it may see it
   → the client is told what changed and re-reads the ticket over HTTP
```

**Where each piece lives.** `app/websocket/events.py` is the vocabulary and the boundary: the
envelope models, `channel_for`, the `REALTIME_FOR_EVENT` mapping from the timeline's event types
onto the wire's, and one pure predicate. `app/websocket/manager.py` is the transport: the
per-organization registry of connections, `publish`, and `subscribe_forever`, which is started as
a task in `app/main.py`'s lifespan so that it has exactly one lifetime. `app/api/websocket.py`
owns the single route, `/ws`, mounted at the application root because `vite.config.ts` proxies
that path unchanged.

Connections authenticate before joining, and subscription is bound to the organization in the
verified token — a client cannot subscribe to another organization's channel. Because fan-out goes
through Redis rather than process memory, any API instance can serve any client, which keeps
horizontal scaling intact.

Publication happens after commit, so clients are never notified of a change that later rolls back.

**Two corrections to the plan as written above.** "Subscription" was the sketch, and it implied a
client may name a channel. It may not: the protocol has exactly one client-to-server message, the
auth frame, and the server places the connection on its own organization's channel. The second is
the audience for a notification. This section originally left room for a channel per user; the
implementation filters **per user on the organization's channel** instead, because
`notify_for_event` and `notify_sla_alert` already decided who each notification is for and that
decision is durable in the row. A channel per person would multiply the subscription set by the
tenant's headcount to buy one comparison, and it would move an authorization decision into a
connection's channel list, where it becomes a second surface to get right.
`notification_visible_to` compares the addressee and deliberately does not re-derive the audience.

**The boundary is one predicate with two axes**, and the second is the one worth naming here: an
envelope carrying `internal: true` requires `MESSAGE_READ_INTERNAL`, because row scope alone would
have pushed a customer the internal note written about them. The publisher states the fact and the
predicate decides the audience — the division `notification_service` already follows.

**A slow client is dropped, not queued.** The subscriber never awaits a socket: each connection
owns a bounded queue and its own writer task, and overflow closes that connection with `1013`.
Without it, one stalled TCP connection would delay every tenant on the instance. No application
heartbeat is sent; uvicorn's protocol-level ping already answers that.

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

Five read-only routes under `/api/v1/analytics` are what that serves: `overview` and
`agents` wholly, `sla` in its aggregate half, and `tickets` and `sentiment` not at all — each one
grouped over the indexes Phase D built for its predicates, over the same row scope every ticket read
uses, so an administrator gets the organization's numbers and an agent gets their own from the same
URL.

**A key carries four things**, because leaving out any one of them would serve a caller something
that is not theirs: the tenant, the row scope (`org`, or `user:{id}` for a caller whose scope is
their assigned work), the version integer, and a digest of the query parameters. The scope token is
the one worth naming — a key of `(tenant, metric, range)` would pass every cross-tenant test and
still hand one caller another's numbers *inside* a tenant, which is why it is derived from
`TICKET_SCOPE_BY_ROLE` rather than from a role name.

**Invalidation is a version integer, not a key sweep.** A ticket write `INCR`s
`analytics:version:{organization_id}` and every key embeds that number, so one increment makes every
existing entry unreachable at once; the entries themselves are orphaned and expire with their TTL.
Redis cannot delete a pattern of keys without `SCAN`, and `KEYS` is banned in production. There are
eight writers — the seven ticket actions, and an SLA policy edit, which moves every SLA aggregate by
changing a join condition rather than a ticket column.

**The payload is validated on the way back out.** Redis is not a trusted store: a value that does not
parse as the response model is a **miss** and is recomputed, which is what makes a deploy that
changes a response shape safe rather than a source of `500`s. Every path fails open — a Redis outage
means "compute it", never a failed request — at the cost that a stale entry can survive the outage
until its TTL expires, which is a cost the README records rather than hides.

**`GET /analytics/sla` is cached in one half and not the other.** Compliance and the past-due count
are aggregates and are shared for a few minutes; the risk list is computed per request, because
`remaining_seconds` is a function of now and a cached countdown is a wrong countdown. ADR-026 has the
full argument, including the one SQL expression for the deadline instant — the seam where the query
language meets the pure clock — and
`tests/integration/test_analytics_sla_agreement.py`, the differential test that keeps the two in
step.

## 11. Deployment shape

Local development runs infrastructure (Postgres, Redis, MinIO, Mailpit) in Docker
while the API, worker, and frontend run natively for fast reloads. Full-stack Compose
files exist for parity checks and CI.

Production runs the same container images behind a load balancer: multiple API
instances, separate worker and beat processes, managed Postgres and Redis, and
object storage. Migrations run as a discrete step before the new backend rolls out.
The target platform is deliberately undecided; nothing in the architecture depends on
a specific cloud.
