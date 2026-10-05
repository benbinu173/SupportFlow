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

The lower half of this section is implementation, and every operation the provider boundary declares now has a
line on it. **Phase T (ADR-027)** built the provider abstraction, the call path, and the ledger. **Phase
U (ADR-029)** built §18's flow around them for the first two operations — classification and sentiment —
with the route, the queue, and the worker. **Phase V (ADR-030)** added §20's summary on the same worker
and a rule that decides when it does not need to run at all. **Phase W (ADR-031)** added §21's suggested
reply. **Phase X (ADR-032)** added §22's embeddings and §23's grounded answer — a *second* vendor behind
the same interface — and with it §21's retrieval step, which was the one line of diagram this section had
been missing since Phase B.

```
trigger (ticket created, or agent request)          ── ✅ U; ✅ V by request, when the conversation moved
   → enqueue task, return immediately                    ── ✅ U, on the `ai` queue; ✅ X, on the `knowledge` queue
   → worker loads ticket within tenant scope             ── ✅ U (`ai_repository.py`, tenant in the WHERE)
   → build prompt from a versioned template              ── mechanism in T (`app/ai/prompts.py`); classification and sentiment text in U; summarization text in V; reply text in W; grounded-answer text in X
   → provider call with timeout and bounded retry        ── ✅ T, for generation and for embeddings alike
   → parse into a Pydantic model  ── invalid ──▶ handled failure, no result  ✅ T
   → persist AI_USAGE (one row per attempt)              ── ✅ T; ✅ V records a served-from-storage answer as a row with `was_cached`; ✅ X records an embedding under `EMBEDDING_MODEL`
   → persist AI_ANALYSIS                                 ── ✅ U
   → update ticket AI fields                             ── ✅ U (never `tickets.priority`); a summary writes no field at all
   → publish event → clients update live                 ── ✅ U
```

**The retrieval row §21 was waiting for.** A draft's trigger now reads §22's published passages before it
builds a prompt, which is the same shape as every row above it and is why §7's flow is reachable from
this one: `_draft_request` composes the ticket, the conversation, and up to `RETRIEVAL_TOP_K` passages
into one user message. A tenant with nothing published makes no embedding call at all, and a knowledge
base that cannot be reached does not stop the draft — see §7 and ADR-032 Decision 8.

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

**What Phase W added on top of V.** §21's suggested reply as a **third entry point on the same worker**,
and the one operation whose result a person can put in front of a customer. The draft is **two records
and no new column**: the model's own output goes in an `ai_analyses` row exactly as every other
operation's does, and a second row — a `messages` row with `sender_type = AI_DRAFT`, `is_internal = true`
— puts it in the thread, which is what makes §21's *"explicit UI distinction between AI generated draft
and human-authored response"* a field already on `MessageRead`. `ai_draft_is_internal` (Phase D) makes
the customer half of that structural: a draft that is not internal is refused by the database, not by a
service. **§41's three verbs are a route and an audit trail, and nothing else.** *Regenerate* is asking
again — the request path writes `AI_RESPONSE_REGENERATED` instead of `AI_ANALYSIS_REQUESTED` when a
completed draft already exists, so no second route and no `?force=` is needed, and deliberately **no
freshness rule**: Phase V's rule says a summary is reusable because the same input has one right answer,
while a second draft request is a person asking for a *different* one. *Edit* is the client's text box,
and only the text that actually went out is persisted. *Accept* is its own route under the ticket, not a
field on the reply body, because it carries two capabilities at once — `AI_REQUEST_SUGGESTION` and
`MESSAGE_POST_REPLY` — the way `app/api/messages.py` already refuses to fold a public reply and an
internal note into one endpoint. Accepting **re-authors** the draft: a new `agent` message goes out
through the ordinary reply path, with the same `first_response_at`, notification, socket publish, and
cache invalidation as any other reply, and the draft row stays behind internal and untouched. The
before/after pair in `AI_RESPONSE_ACCEPTED` is where §34's *"where appropriate"* finally has a case it
was written for — the draft's body against the body that was sent, with `edited` saying whether they
differ. And **§21's "relevant knowledge" step was not there**: that retrieval is §22's, and a parameter W
always passed empty would have been a stub with no caller, which is the same reasoning ADR-008 used to
keep `generate_embedding` off the protocol. The change was additive, and Phase X made it — in one
function, as forecast.

**What Phase X added on top of W.** §22's document pipeline, §23's grounded answer, and the retrieval row
that completes §21. **One interface, two vendors, and no third protocol where none was needed.** §17's
five methods are served by `AIProvider` (the generation calls) and `EmbeddingProvider` (the fifth) —
ADR-008's separate-vendor decision arriving, and ADR-027's deferral discharged on the protocol whose
implementations can answer it, since neither Anthropic nor Groq publishes an embedding model. §23's
grounded answer is a **sixth operation on `AIProvider`** and not a fourth protocol: it is prose plus the
passages it used, every generation vendor serves it identically, and a protocol exists where
implementations differ. `AIResult` and `ai_service._run` are reused unchanged — the retry policy, the
jittered backoff, the one-ledger-row-per-attempt rule — so a genuinely second vendor runs the same code
Phase T wrote for the first, which is the strongest available evidence that the boundary was drawn in the
right place. **Ingestion is the first AI work on its own queue** (`knowledge`, ADR-032 Decision 5), the
first that has no ticket behind it, and the first whose ledger rows are committed on the failure path by
a claim that has to survive a redelivery: the document row is flipped to `processing` *before* the first
network call, so a task that dies and is redelivered finds a row it may not claim rather than a second
embedding bill.

The full flow, the guard that makes a URL document safe to fetch, and the two branches of §23's question
are §7's subject.

**What runs today.** `app/services/ai_service.py` is the single call path — `classify_ticket`,
`analyze_sentiment`, `summarize_conversation`, `generate_response`, `answer_question`, and `embed_texts`
— and every one of them reduces to a private `_run`: select the provider, call it with the client's
timeout, retry a *transient* failure up to `AI_MAX_ATTEMPTS` with jittered backoff, validate the answer
into its schema, and stage one `ai_usage` row for every attempt including the failed ones. The provider
is chosen by `AI_PROVIDER` (and the embedding vendor by `EMBEDDING_PROVIDER`), and **each vendor gets
exactly one module, which is the only thing in the project that imports its client** —
`app/ai/claude.py` for the `anthropic` SDK, `app/ai/groq.py` and `app/ai/openai_embedding.py` for
`httpx`. Three real vendors behind two protocols is what makes the boundary checkable rather than
asserted: the second and third each cost a module and changed nothing above them (ADR-028, ADR-032).

Three invariants, and how each is held:

- **Provider-agnostic.** Application code depends on the `AIProvider` protocol
  (`classify_ticket`, `analyze_sentiment`, `summarize_conversation`, `generate_response`,
  `answer_question`) and, for the fifth method, on `EmbeddingProvider` — and never on a vendor SDK.
  `generate_embedding` is deliberately **not** on `AIProvider`: ADR-008 put embeddings behind the same
  interface with a *different* vendor, and declaring the method there would give `ClaudeProvider` and
  `GroqProvider` a stub neither can ever serve. It arrived in Phase X, on the protocol whose
  implementations can answer it. Both protocols share `Provider`, which declares the one attribute
  `_run` needs — `name` — so the retry loop ledgers either kind of call with no branch in it.
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

Phase X is the first phase whose spend is **not** attached to a ticket: an ingestion's embedding rows
carry `ticket_id = NULL`, because a document belongs to the organization rather than to a conversation
and nothing asked for it. A draft's retrieval is the other case and does carry a ticket, because a
ticket is what asked. And §23's refusal branch is the one branch in the system that deliberately writes
**no** row at all — a `was_cached` entry would be a lie, since nothing answered from a cache and the
call was never going to be made.

**Two obligations travel with the ledger, and both belong to the caller.** `ai_service` stages rows
and never commits, so whoever calls it commits — including after a failure, since a ledger that
rolled back with a failed request would be missing exactly when it matters. And because
`/analytics/overview` is cached while an `ai_usage` write does not bump the version integer, whoever
commits also owns the `cache.invalidate` — the same pattern `ticket_service` follows after its own
commit. Phase U's route is where both obligations land; Phase X's ingestion task commits its own rows
on the *failure* path too, because a failed embedding is still spend.

Failure is contained: a provider that is down, throttled, or holding a bad key raises
`AIServiceError` (503) and nothing else escapes, so a caller can treat every AI failure as one
condition. **Phase X is the first phase with an AI call on a request path** —
`POST /knowledge/search` embeds the question in the request, because a question is asked by a person
who is waiting for its answer — and it is rate limited by `limit_ai` for exactly that reason. Every
other AI call is still behind a queue, and a drafting request that cannot reach the knowledge base
proceeds without it rather than failing (ADR-032 Decision 8).

## 7. RAG flow

Implemented in Phase X (ADR-032). This section was written in Phase B as a plan; it is now a description
of what runs.

**Ingestion** (asynchronous, per document, on the `knowledge` queue):

```
POST /knowledge | /knowledge/upload   (text, a URL, or a file)
   → validate → object storage (uploads only) → metadata row (status=pending)
   → worker: extract text → clean → chunk with overlap
   → embed each batch → persist chunks + vectors → status=completed, is_published=true
```

A URL is **not** fetched in the request: the row is written with the URL as its reference and the worker
fetches it under the guard in `app/services/url_fetch.py`, so a request cannot be made to wait on a
stranger's server. The row is committed as `processing` *before* the first network call, so a task that
dies and is redelivered finds a row it may not claim rather than a second embedding bill. A failure —
a refused address, a provider outage, a document that extracts to nothing — leaves `status=failed` with
a reason an admin can read, and never a half-indexed document that looks complete.

**Query:**

```
question → has anything been published?  ── no ──▶ the refusal, and no provider call at all
   │ yes
   → embed → vector search WHERE organization_id = ctx.organization_id
   → top-k chunks above a relevance threshold (RETRIEVAL_TOP_K, RETRIEVAL_MIN_SIMILARITY)
   │
   ├─ nothing clears threshold → "knowledge base lacks this information", sources: []
   └─ chunks found → grounded prompt → answer + real source references
```

Vector search uses pgvector with an HNSW index under cosine distance. The tenant predicate is part of
the query, so retrieval cannot cross organizations — the same isolation guarantee as every other read,
applied to the vector path. **The claim is asserted on the result rather than on the query**:
`tests/security/test_knowledge_isolation.py` lays a question onto one tenant's chunk *exactly* — making
it the nearest neighbour in the whole table by arithmetic — and asserts that the other tenant's search
returns its own document's id, that the stranger's text is absent from the response body, and that the
refusal is byte-identical to a question whose document never existed.

Grounding rules: answer only from retrieved context, state gaps explicitly, never invent policy, never
fabricate a citation. Sources shown in the UI resolve to real stored chunks — `KnowledgeSource.excerpt`
is the passage's own text rather than a summary, so a citation is checkable rather than decorative.

**Two deliberate divergences from the plan as written above**, both recorded in ADR-032.

- **`status=ready` is `status=completed`.** Phase B's sketch used the word §22's prose uses; the enum
  Phase D declared has four members, and the retrieval predicate is the one the partial index encodes:
  `is_published = true AND status = 'completed'`. The worker sets both, which is what §22's *"document
  becomes searchable"* means as a stored fact.
- **The no-clears path makes no model call.** The plan reads as though a model always answers. It does
  not: when nothing clears the threshold the refusal is this server's own sentence, there is no model in
  the loop to answer from its priors, and **no ledger row** is written — a `was_cached` entry would
  claim a saving that was never available. The `EXISTS` guard above it is what keeps a tenant that has
  never opened the knowledge base from paying for a vector it cannot use.

**§21's drafts read this flow too.** The retrieval is the same call, the query is the ticket's own words,
and the passages arrive as an unnumbered third block in the draft prompt — a draft cites nothing, so a
number in a reply an agent may send onward would be noise at best. A knowledge outage does not stop
drafting, and a tenant with nothing published makes no embedding call, which is what keeps §21's
behaviour for such a tenant exactly what it was before this phase.

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

**Four queues, one per kind of work**, so a slow job cannot delay a fast one: `ai` (the ticket
operations), `knowledge` (document ingestion), `sla`, and `notifications`. Priorities within a queue come
from the broker rather than from the task. The two terminal states a task can find are both stated on
the row rather than in the task: an analysis that finished is `completed` and re-running it does
nothing, and an ingestion that is not `pending` is skipped — stricter than the analysis path, because
two workers appending the same document's chunks would collide on a unique index, and a crash
mid-ingestion is recovered by deleting the document and uploading it again rather than by a second
half-indexed run (ADR-032 Decision 5).

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
