# SupportFlow

> AI-assisted customer support, built for modern support teams.

A multi-tenant customer support and helpdesk platform. Support teams work tickets
through a validated lifecycle, assisted by AI for classification, sentiment,
summarization, and draft replies — with a human always in the loop before anything
reaches a customer. Each organization keeps a private knowledge base that grounds AI
answers in its own documented policy rather than model invention.

**Status: Phase C of 26.** Repository, tooling, documentation, containerized
infrastructure, and CI are in place. Domain features are not built yet — see
[Roadmap](#roadmap).

## Why this project exists

A demonstration of production-style Python backend engineering: multi-tenant data
isolation proven by tests, asynchronous processing with real work behind it,
retrieval-augmented generation scoped per tenant, and real-time updates that survive
horizontal scaling. The emphasis is architectural quality and security over feature
count.

## Technology

| Layer | Choice |
|---|---|
| Frontend | React 19, TypeScript, Vite, Tailwind 4, TanStack Query, Zustand, React Router |
| Design | Premium agency visual language — Geist / Plus Jakarta Sans, Phosphor light icons, Motion (ADR-010) |
| Backend | Python 3.14, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic |
| Database | PostgreSQL 17 + pgvector |
| Cache / messaging | Redis 8 |
| Background jobs | Celery + beat |
| AI | Provider-agnostic interface; Claude for generation, separate embedding provider |
| Storage | S3-compatible (MinIO locally) |
| Testing | pytest, Vitest, Testing Library |
| Infrastructure | Docker Compose, GitHub Actions |

Library choices that deviate from convention — Argon2id over bcrypt, PyJWT over
python-jose, opaque refresh tokens over JWTs — are justified in
[docs/architecture-decisions.md](docs/architecture-decisions.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/requirements.md](docs/requirements.md) | Actors, full permission matrix, workflows, lifecycle, success criteria |
| [docs/architecture.md](docs/architecture.md) | Component diagram, layering, auth / tenancy / AI / RAG / real-time / job flows |
| [docs/data-model.md](docs/data-model.md) | ERD, table reference, index strategy, schema-level business rules |
| [docs/architecture-decisions.md](docs/architecture-decisions.md) | Every deviation from the specification, with reasoning and cost |

## Local setup

**Prerequisites:** Docker Desktop, Python 3.12+, Node 22+.

```bash
# 1. Environment
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into JWT_SECRET

# 2. Infrastructure — Postgres, Redis, MinIO, Mailpit
docker compose up -d

# 3. Backend
cd backend
python -m venv .venv
source .venv/Scripts/activate        # Windows; use bin/activate on macOS/Linux
pip install -e ".[dev]"
uvicorn app.main:app --reload

# 4. Frontend (new terminal)
cd frontend
npm install
npm run dev
```

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/health |
| MinIO console | http://localhost:9001 |
| Mailpit | http://localhost:8025 |

Infrastructure runs in Docker while application processes run natively — Celery's
prefork pool is unavailable on Windows and bind-mount file watching degrades reload
performance. Rationale in ADR-007. For container parity:

```bash
docker compose --profile full up --build
```

## Environment variables

All configuration is environment-driven; see [.env.example](.env.example) for the full
template. Secrets have no defaults — the application refuses to start without them
rather than falling back to something insecure.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection (required) |
| `REDIS_URL` | Rate limiting today; db 0. The Celery broker uses db 2 (required) |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Redis db 2 — a separate database from the cache, so a `FLUSHDB` cannot take out a queue |
| `SMTP_*` | Mail server. Defaults to Mailpit on `localhost:1025`; `SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_STARTTLS` for a real provider |
| `SLA_SWEEP_INTERVAL_SECONDS` | How often beat runs the deadline sweep (default 300). It is the knob that decides how late a warning can be: at URGENT's 24-minute warning band it bounds the delay at 5 minutes |
| `SLA_SWEEP_BATCH_SIZE` | Tickets per priority per sweep (default 500) |
| `RATE_LIMIT_*` | Per-address login/registration limits and the per-user upload limit |
| `JWT_SECRET` | Access-token signing; minimum 32 characters (required) |
| `CORS_ORIGINS` | Comma-separated allowlist. No wildcard — the refresh cookie needs credentials |
| `AI_API_KEY` / `AI_MODEL` | Generation provider |
| `S3_*` | Object storage for attachments |

## Commands

```bash
# Backend (from backend/)
pytest                      # tests
pytest -m security          # tenant-isolation and authz tests only
ruff check . && ruff format --check .
mypy app alembic

# The worker, needed for any email to actually be sent and for SLA alerts to fire.
# `-Q` must name *every* queue in `task_routes` — see the SLA section for why a worker
# that misses one looks perfectly healthy and silently consumes nothing.
celery -A app.workers.celery_app worker --loglevel=info --pool=solo -Q notifications,sla

# Beat, which is the clock the SLA sweep runs on. Exactly one process: two would
# double every sweep. The explicit schedule path is not optional — the default writes
# into the working directory, and a beat that cannot write it fails at startup.
celery -A app.workers.celery_app beat --loglevel=info -s /tmp/celerybeat-schedule

# End-to-end against a running server, including the parts TestClient cannot show
# (real multipart, real streaming, real headers). Needs the API on :8000.
python scripts/phase_ln_walkthrough.py

# The same, for notifications: needs the API on :8000, the worker above, and Mailpit.
# Reads the delivered message back out of Mailpit's API at :8025 — see the Notifications
# section for the full process list.
python scripts/phase_p_walkthrough.py

# And for SLAs: the same setup plus beat. A ticket is aged in SQL — the one thing no
# endpoint exposes — the sweep is invoked the way beat invokes it, and the alert email
# is read back out of Mailpit. Running the sweep twice is part of the script.
python scripts/phase_q_walkthrough.py

# Frontend (from frontend/)
npm test
npm run typecheck
npm run build
```

A [Makefile](Makefile) wraps the common combinations (`make test`, `make check`,
`make ci`).

## Database migrations

Schema changes go through Alembic — `Base.metadata.create_all` is used by the test
suite only, never to evolve a real database.

```bash
make migrate              # apply everything outstanding
make migration m="add ticket tags"   # autogenerate from model changes
make downgrade            # revert the most recent migration
make migration-sql        # print the DDL without running it
make migration-check      # fail if models and migrations have drifted
```

Equivalent direct commands, from `backend/`:

```bash
alembic upgrade head
alembic revision --autogenerate -m "add ticket tags"
alembic downgrade -1
alembic upgrade head --sql
alembic check
```

Notes:

- **There is no database URL in `alembic.ini`, deliberately.** The DSN carries a
  password and that file is committed. Alembic reads `DATABASE_URL` through
  `app.core.config`, so `alembic.ini` cannot drift from the application's own
  connection settings. To target a different database, set `DATABASE_URL` in the
  environment.
- **Autogenerated migrations are a draft, not a result.** Read the generated file
  before committing it. Autogenerate cannot see enum value removals, data
  migrations, or constraint changes that need a backfill.
- **Every migration must be reversible.** `upgrade head` then `downgrade base` is
  asserted in [tests/integration/test_migrations.py](backend/tests/integration/test_migrations.py),
  along with a drift check that fails when a model change has no matching migration.
  Native enum types are the usual trap: they outlive the tables that used them, so a
  downgrade has to drop them explicitly.

## Authentication

Email and password, with Argon2id hashing (ADR-001) and short-lived JWT access tokens
(ADR-002). Five endpoints, all under `/api/v1/auth`:

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /register` | public | Creates an organization **and** its first administrator |
| `POST /login` | public | Returns an access token; sets the refresh cookie |
| `POST /refresh` | cookie | Rotates the refresh token; returns a new access token |
| `POST /logout` | access token + cookie | Revokes the presented refresh token and clears the cookie |
| `GET /me` | access token | The caller's own profile |

**Access tokens** are HS256 JWTs carrying `sub`, `org`, `role`, and `exp`, valid for
`ACCESS_TOKEN_EXPIRE_MINUTES` (default 15) and returned in the response body for the
client to hold in memory.

**Refresh tokens are opaque random strings, not JWTs** (ADR-003). Only a SHA-256 hash
is stored, so a database dump yields no usable session. They are set as an `HttpOnly`,
`SameSite=Lax`, `Secure` cookie scoped to `Path=/api/v1/auth` and are **never**
returned in a response body. That cookie attribute is also the CSRF control — a
cross-site POST does not carry it (ADR-014).

Refreshing **rotates**: the presented token is revoked and a new one issued. Presenting
an already-used token is treated as theft and **revokes every live session for that
user** — not merely the token's own chain. A replayed token proves the theft but not
which holder is the thief, so the safe reading is "one of this user's sessions is
compromised, and we do not know which". Every other device has to sign in again, which
is a small cost against an attacker keeping a working session.

### The token is an identifier, not an authority

On every request the dependency verifies the signature, loads the user, refuses if the
user is inactive, and then **uses the role from the database row** — not the claim in
the token. A token whose `org` disagrees with the user's current `organization_id` is
refused with `TENANT_ACCESS_DENIED`.

The practical effect: a demotion, a promotion, or a deactivation takes effect on the
**next request** rather than up to 15 minutes later. ADR-013 records the trade-off.

```bash
# A complete session, against a running server
curl -sX POST localhost:8000/api/v1/auth/register -H 'content-type: application/json' \
  -d '{"organization_name":"Acme","name":"Ada","email":"ada@acme.com","password":"correct-horse-battery-staple"}'
# → 201 {"access_token":"eyJ...","token_type":"bearer","expires_in":900}  + Set-Cookie: sf_refresh=...

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN"
curl -sX POST localhost:8000/api/v1/auth/refresh -b "sf_refresh=$REFRESH"
```

## Authorization

Permissions are **capabilities**, declared per route:

```python
dependencies=[Depends(require_permission(Permission.USER_CREATE))]
```

`ROLE_PERMISSIONS` in [app/core/permissions.py](backend/app/core/permissions.py) is the
single source of truth, transcribed row by row from
[docs/requirements.md](docs/requirements.md) §3. Four roles:

| Role | Scope of what it can reach |
|---|---|
| `admin` | Everything in the organization, including users and settings |
| `manager` | Assigns and reprioritises tickets; cannot administer users |
| `agent` | Works tickets assigned to them |
| `customer` | Sees only their own tickets |

`own` and `assigned` are **row scopes, not permissions** — a separate central mapping
applied where the query is built, because a route-level check has no row to examine
yet. ADR-013 explains why the two concepts are kept apart.

No route anywhere contains a role comparison. That is enforced mechanically, not by
review: `tests/unit/test_permissions.py` scans every module outside
`app/core/permissions.py` for `UserRole.ADMIN`-style references and `role == "admin"`
string comparisons, against a short justified allowlist. `tests/security/` walks the
routing table and fails if any route is neither authenticated nor explicitly public,
and if any protected route declares no capability.

A guard the caller fails returns `403 FORBIDDEN`.

## Multi-tenancy

**Every tenant-scoped table carries `organization_id`, and the only source of that
value is the authenticated user's database row.** `TenantContext` is constructed in
[app/api/deps.py](backend/app/api/deps.py) and nowhere else; it is never read from a
header, body field, query parameter, or path segment. There is no request a client can
make that influences which organization it is served as — a test asserts that posting
`organization_id` (and `organizationId`) to the user-creation endpoint changes nothing.

Isolation is enforced at the three layers [docs/architecture.md](docs/architecture.md) §5
specifies, plus one cross-check:

1. **Context** — the request's organization comes only from the authenticated
   principal. `TenantContext` is built in one place, from the user's database row.
2. **Repository** — `TenantScopedRepository` takes a `TenantContext` at construction
   and injects the `organization_id` predicate into every query. A route cannot
   accidentally query across tenants, because it never holds an unfiltered session;
   writing a tenant-scoped query without the filter requires bypassing the repository.
3. **Schema** — every tenant-owned table carries `organization_id` with a foreign key
   and an index, and uniqueness is scoped per organization (so the same email may exist
   in two of them). The constraints are in [docs/data-model.md](docs/data-model.md).
4. **Cross-check** — a token naming a different organization than the user's row is
   refused outright with `TENANT_ACCESS_DENIED`, rather than trusted. A `403` here,
   against a `404` for cross-tenant reads, because a token that should not exist is a
   different condition from a record that is out of reach.

### Cross-tenant reads return 404, not 403

A `403` would confirm that the record exists and is merely out of reach, turning the
API into an enumeration oracle (ADR-009). A `404` is byte-for-byte identical to the
response for an id that never existed, which is asserted directly — status, error code,
message, and full body are compared — so nothing about the other tenant's data leaks.

The same reasoning applies to a refused write: the tests follow every cross-tenant
`404` with proof that nothing happened, by re-reading the row from the owning
organization and, for a deactivation, by the victim logging in again.

### What is not yet tenant-scoped

Everything the API exposes now is tenant-owned. `organizations` is the tenant itself,
and `refresh_tokens` is read by token hash before a tenant is known — neither is a
resource a client can list. Knowledge articles, attachments, and audit logs arrive in
Phases L–N and follow the identical repository pattern. See
[docs/data-model.md](docs/data-model.md).

## Customers, tickets, and messages

The product itself. Phases I–K mount three routers, 16 routes, all of them
authenticated and all of them capability-guarded.

| Route | Capability |
|---|---|
| `POST /api/v1/customers` | `CUSTOMER_CREATE` |
| `GET /api/v1/customers?q=&limit=&offset=` | `CUSTOMER_LIST` |
| `GET`/`PATCH /api/v1/customers/{id}` | `CUSTOMER_LIST` / `CUSTOMER_UPDATE` |
| `POST /api/v1/tickets` | `TICKET_CREATE` |
| `GET /api/v1/tickets?status=&priority=&assigned_agent_id=&customer_id=` | `TICKET_LIST` |
| `GET /api/v1/tickets/{id}` and `/events` | `TICKET_VIEW` |
| `POST /api/v1/tickets/{id}/assign` | `TICKET_ASSIGN` |
| `POST /api/v1/tickets/{id}/priority` | `TICKET_CHANGE_PRIORITY` |
| `POST /api/v1/tickets/{id}/status` | `TICKET_CHANGE_STATUS` |
| `POST /api/v1/tickets/{id}/close` | `TICKET_CLOSE` |
| `POST /api/v1/tickets/{id}/reopen` | `TICKET_REOPEN` |
| `GET /api/v1/tickets/{id}/messages` | `MESSAGE_READ_PUBLIC` |
| `POST /api/v1/tickets/{id}/messages` | `MESSAGE_POST_REPLY` |
| `POST /api/v1/tickets/{id}/notes` | `MESSAGE_POST_INTERNAL` |

`GET /customers/{id}` requires `CUSTOMER_LIST` rather than a separate view capability,
mirroring `/users/{id}` → `USER_LIST`: the matrix has no `CUSTOMER_VIEW` row, and
inventing one would put the route table out of step with it.

### Row scope is applied where the query is built

`own` and `assigned` cannot be checked at the route, because at the route there is no
row yet. So `TICKET_SCOPE_BY_ROLE` is applied by the repository that builds the query —
`organization_id = :org AND assigned_agent_id = :me` for an agent, `… AND customer_id
= :me` for a customer. The result is a **404, not a 403**, for a ticket outside the
caller's scope, the same as for another tenant's: an agent should not be able to map a
colleague's workload by watching which ids are refused.

The filter parameters are **narrowing only** and compose with the scope rather than
replacing it. An agent asking for `?assigned_agent_id=<someone else>` gets an empty
page, not that agent's queue — asserted both at the repository and over HTTP in
[tests/security/test_row_scopes.py](backend/tests/security/test_row_scopes.py), because
"a query parameter that widens access" is the classic version of this bug.

A portal account with **no linked customer** reaches nothing. That state cannot be
created through the API — `POST /users` refuses a portal role without a `customer_id` —
so the test constructs the context directly and asserts the empty page. The predicate is
written out as an explicit `false()` rather than left to `customer_id = NULL`, which is
*unknown* rather than false and happens to match nothing by accident. ADR-015 has the
reasoning.

Messages are resolved **through their ticket**, not by a second copy of the scope.
Every message route first loads the ticket via `TicketRepository`, which already applies
the caller's scope; only then are messages read. `MESSAGE_SCOPE_BY_ROLE` stays as the
declaration of intent, and the repository that enforces it is the ticket's.

### Status is an action, never a field update

There is no `PATCH /tickets/{id}` for status. Three routes, one capability each, each
validating an edge of `TICKET_TRANSITIONS`:

```
OPEN ──assign──▶ ASSIGNED ──status──▶ IN_PROGRESS ⇄ WAITING_FOR_CUSTOMER
  ▲                                        │
  │                                        └──status──▶ RESOLVED ──close──▶ CLOSED
  └────────────────────────── reopen ─────────────────────────────────────────────┘
```

`/status` is the general write and accepts any single edge the table allows; `/close`
accepts `RESOLVED → CLOSED` and `/reopen` accepts `CLOSED → OPEN`, and nothing else.
Splitting them is what keeps the route→capability map 1:1 with
[docs/requirements.md](docs/requirements.md) §3 — a route that picked its required
capability at request time could declare none, which the route-protection test would
correctly reject.

`/close` accepting only `RESOLVED → CLOSED` is deliberate: that matrix row is "confirm
resolution", and a customer confirming is not the same act as an agent resolving. So
`ASSIGNED → RESOLVED` is refused with `409 INVALID_TICKET_TRANSITION` and a `hint`
naming the legal target — a ticket has to be worked before it can be resolved.

Every transition writes a `TicketEvent` in the same transaction, and closing or
resolving sets `resolved_at`/`closed_at` in the same statement — a `CheckConstraint`
makes forgetting an integrity error rather than a silent inconsistency. **Reopening
clears the assignment and both timestamps**: a ticket in `OPEN` with an agent set is a
state the rest of the model has no reading for, and leaving `resolved_at` populated
would make every duration query wrong.

### Internal notes are hidden twice, on purpose

`GET /tickets/{id}/messages` returns the thread oldest-first, and includes internal
notes only for a caller holding `MESSAGE_READ_INTERNAL` — an agent or above. The
database backs this with a `CheckConstraint` on `is_internal`, and the repository
excludes internal rows by default, so a missing filter hides rather than leaks.

The timeline hides them too. A customer who can see that a note was written at 14:02,
and not what it said, has still learned something the thread filter exists to hide — so
`INTERNAL_NOTE_ADDED` events are filtered out of `GET /tickets/{id}/events` for a caller
who cannot read notes, under the same capability. Without that, the two views of one
ticket contradict each other.

### Ticket numbers are per organization, under an advisory lock

`tickets.number` starts at 1 in each organization and there is no sequence behind it.
Allocating it as `MAX(number) + 1` would collide under concurrency, and a retry loop
around the resulting integrity error needs a `SAVEPOINT`, a bounded attempt count, and a
test for the exhaustion path. Instead the allocation takes
`pg_advisory_xact_lock` keyed on the organization, then reads the maximum. The lock is
transaction-scoped, released on commit or rollback, and per-tenant, so one busy tenant
never blocks another. ADR-016 records the trade.

The unique index on `(organization_id, number)` stays: the lock is the mechanism, the
index is the invariant. Verified by creating eight tickets from eight threads against one
organization and asserting the numbers are exactly 1–8.

## Attachments

A ticket can carry files. Spec §33's flow is
`Frontend -> FastAPI -> validated upload -> object storage -> database metadata`, and all
four arrows go through the API:

```
POST /api/v1/tickets/{ticket_id}/attachments   multipart, optional message_id form field
GET  /api/v1/tickets/{ticket_id}/attachments   metadata, newest first
GET  /api/v1/attachments/{attachment_id}       the bytes, streamed
```

**Uploads are proxied; there are no presigned URLs.** A presigned URL is handed to the
client and then fetched without the API in the path, so authorization cannot be enforced
on it — the tenant scoping that applies to every row would stop applying at the one point
where the bytes actually leave. Proxying is the only shape that satisfies §33's last
line, and ADR-018 records what it costs.

**The storage key is server-generated**: `{organization_id}/{ticket_id}/{uuid4().hex}`.
The client's filename never reaches it, so path traversal is impossible by construction
rather than by sanitizing — §33's "prevent path traversal" is satisfied by the shape of
the key, and the test asserts that shape rather than searching it for `..`. The filename
is kept for display, with its directory stripped.

**Validation refuses three disagreements.** Allowed types are PNG, JPEG, GIF, PDF, and
plain text. For each upload the extension, the declared `Content-Type`, and the bytes'
leading signature must agree *and* the signature must actually have been read — which is
§33's "never trust filename or MIME type alone" taken literally, since all three inputs
are client-controlled. `text/plain` has no signature, so it is accepted only when the
extension and the declared type agree; that hole is stated in the module rather than left
implicit. Size is enforced by counting bytes as they stream, not by trusting
`Content-Length`, because a header is a claim.

**A file on an internal note is internal.** An attachment has no `customer_id` and no
`assigned_agent_id` of its own, so it has no row scope to take — it is reached through
its ticket, and every attachment route resolves the ticket first with the same scope
predicate the ticket routes use (ADR-019). On top of that, one row rule: an attachment
whose `message_id` names an internal note requires `MESSAGE_READ_INTERNAL`, and without
it the attachment is a **404 indistinguishable from one that never existed**. The
capability is not what stops a customer here — every role holds `ATTACHMENT_UPLOAD` and
`ATTACHMENT_DOWNLOAD` — the row scope is.

Downloads are streamed with the **detected** content type, a `Content-Length` from the
stored size, `Content-Disposition: attachment` with the sanitized filename, and
`X-Content-Type-Options: nosniff` — without which a browser may render a stored file as
HTML in the API's own origin.

## Audit log

`GET /api/v1/audit-logs` reads back what §34 asks to be recorded: who did what, to which
object, when, and from where. It is **admin-only**, ordered newest first with `id` as the
tiebreak so the page boundary is stable, filterable by `action`, `actor_user_id`,
`target_type`, `target_id`, and a half-open date range, and paginated like every other
list.

| Action | Written by |
|---|---|
| `USER_CREATED` | `POST /users`, and registration for the founding admin |
| `USER_ROLE_UPDATED` | `PATCH /users/{id}` |
| `USER_DEACTIVATED` | `POST /users/{id}/deactivate` |
| `TICKET_CREATED` | `POST /tickets` |
| `TICKET_ASSIGNED` | `POST /tickets/{id}/assign` |
| `TICKET_PRIORITY_CHANGED` | `PATCH /tickets/{id}/priority` |
| `TICKET_STATUS_CHANGED` | every lifecycle transition |
| `TICKET_RESOLVED` | the transition *into* `RESOLVED` |
| `TICKET_REOPENED` | `POST /tickets/{id}/reopen` |

**Rows are written in the actor's own transaction.** The service never commits, so the
audit row and the change it describes commit together or not at all — a trail that can
disagree with the data it audits answers no compliance question (ADR-020). `before` and
`after` values land in `extra_data` under those keys, mirroring `ticket_events`, so the
two views of one change agree.

**Registration is itself an audited action**, so a fresh organization's trail is never
empty: it holds exactly one `user_created` row for its founding administrator. That is a
deliberate consequence of auditing the endpoint rather than seeding the table, and it is
what `tests/security/test_tenant_isolation.py` asserts a second organization cannot see.

The repository exposes `add` and the list methods and nothing else, and `AuditLog` has no
`updated_at` — so "an audit trail that can be edited answers no compliance question" is
checkable rather than merely written down. Customer and message writes are deliberately
*not* audited: `AuditAction` has no member for either and the enum is a PostgreSQL type,
so adding one is a migration, and both are already fully attributable through
`ticket_events`.

## Search, filtering, and sorting

§14's search lives on the two list routes rather than on new ones, so `GET /tickets`
and `GET /customers` keep returning plain arrays.

`q` on `/tickets` is one disjunction over four arms — subject and description as
full-text, the ticket number read as text, message content, and the customer's name or
email as a substring — **ANDed with the caller's row scope**. The direction matters: a
search narrows and can never widen, and the version of this feature that matters is the
one where `q` is a query parameter that grants access. `q` on `/customers` searches name
and email only.

A term is data, not a pattern: `%`, `_`, and `\` are escaped before they reach `LIKE`, so
a customer searching `100%` finds the customer named `100% Cotton` rather than every
customer in the organization. Full-text terms go through `websearch_to_tsquery` rather
than `plainto_tsquery` because it cannot raise on malformed input and it understands
quoted phrases, which is what a support agent types.

`sort` and `order` are enums, so an unknown key is FastAPI's own `422` and the column map
is one mypy can check for exhaustiveness. `/tickets` sorts by `created_at` (default),
`updated_at`, `number`, or `priority`; `/customers` by `created_at` (default) or `name`.
**`priority` sorts by the PostgreSQL enum's declaration order**, so `order=desc` puts
`URGENT` first — correct, and not obvious enough to leave unstated. **`id` is always
appended as the final sort key**: without a unique tiebreak, offset pagination over equal
`created_at` values silently repeats and skips rows, which is a bug that only appears
under load.

`created_after` is **inclusive** and `created_before` is **exclusive**, so adjacent
windows can be walked without a gap or a double count. The tri-state assignee filter
arrives as two parameters — `assigned_agent_id=<uuid>` for one agent's tickets,
`unassigned=true` for the queue with nobody on it, neither for no filter, and **both is a
`422`** rather than a silent pick.

### What the index measurement actually showed

Phase D built `ix_tickets_fts` and no query used it for four phases; Phase N added
`ix_messages_fts`, because message content was the one searched field with no index
behind it. Whether those indexes are *used* is a measurement, not an assumption, and the
measured answer is mixed in a way worth recording.

Against a seeded organization (5,000 tickets, 20,000 messages, 20,000 customers) with
`EXPLAIN (ANALYZE, BUFFERS)`:

| Query | Plan | Time |
|---|---|---|
| ticket full-text arm alone | Bitmap Index Scan on **`ix_tickets_fts`** | 1.5 ms |
| message full-text arm alone | Bitmap Index Scan on **`ix_messages_fts`** | 3.8 ms |
| customer name `ILIKE` alone | **Seq Scan**, index declined | 19.4 ms |
| the full `q` predicate, real term | Index Scan Backward on `ix_tickets_org_created_at`, filter | 42.8 ms |
| the full `q` predicate, term matching nothing | same, **5,000 rows removed by filter** | 103.2 ms |
| `sort=priority desc` | Seq Scan + top-N heapsort | 3.7 ms |
| `sort=name desc` on customers | Seq Scan + top-N heapsort | 18.6 ms |

Three findings came out of this, and none of them was papered over:

- **The four-arm OR does not use either full-text index.** The planner estimates the
  disjunction at roughly 75% of the tenant's rows — it has no selectivity function for
  `@@` against a generic `tsquery`, and the two `EXISTS` arms are hashed subplans of
  unknown selectivity — and at that estimate walking the tenant's `created_at` index in
  sort order and filtering is the right call. It is fine at these sizes and it is
  *linear in the tenant's ticket count*, which the typo row shows: a term matching
  nothing still costs a full pass over the organization's tickets. The reachable fix is
  to union the arms instead of OR-ing them, so each arm is index-driven and the result is
  deduplicated — a real change to the query shape, deferred rather than smuggled in. The
  individual arms do use their indexes, which is what makes the union plausible.
- **`ix_customers_name_trgm` exists and works, and the planner declines it.** Forced
  with `enable_seqscan = off` it runs in 3.1 ms against the seq scan's 18.6 ms, but the
  planner's estimate is 928 versus 608 the other way. PostgreSQL's GIN cost model is
  pessimistic here; the index will be chosen as the table grows, and nothing needs doing.
- **`ix_customers_org_name` is not worth adding.** `sort=name` is 18.6 ms for 20,000
  customers and the Sort node is 50 of the plan's 1,301 cost — the scan dominates, not
  the sort, which is the stated condition for adding it. So the decision is *no index*,
  made by measurement rather than by taste.

`sort=priority` likewise has no index and does not need one: the existing
`ix_tickets_org_status_priority` leads with `status`, and a query that does not filter by
status cannot use its second column for ordering.

## Rate limiting

§45 names five endpoints to limit — login, registration, AI endpoints, knowledge-base
processing, and file upload. Three of them exist; the other two are built in later phases
and get no speculative setting. Every limit is a fixed window in Redis and returns
`429 RATE_LIMITED` with a `Retry-After` header.

| Endpoint | Key | Window | Default |
|---|---|---|---|
| `POST /auth/login` | client address | 1 minute | 10 |
| `POST /auth/register` | client address | 1 hour | 5 |
| `POST /tickets/{id}/attachments` | **user id** | 1 hour | 60 |

All three guards live in one file, [backend/app/api/rate_limits.py](backend/app/api/rate_limits.py),
so the entire limit surface is auditable in one read — the property that matters for an
abuse control, and the reason both limits are not declared from their own routes.

**Upload is keyed on the user, which is the opposite of login.** At login there is no
identity yet — that is what the endpoint is for — and the attack is credential stuffing
spread across many accounts, so the address is the only key available. Upload is
authenticated: identity exists, and the concern is one account filling the object store.
Keying it on the address would let one person's backlog throttle everyone behind the same
NAT, which is cost #2 ADR-014 already records against the login limiter. That trade is
forced at login and avoidable here, so it is not repeated (ADR-022). Checked against a
running server with the limit set to 2: the third upload from one account is refused, and
a colleague in the same organization — same ticket, same address — is served.

**The refusal happens before the file is examined.** A route dependency is resolved before
the path operation's own parameters, so the guard runs before `file: UploadFile` is read.
Confirmed over real HTTP rather than reasoned about: a request that is *both* over the
limit and carrying a disallowed file type returns `429`, while the identical request from
an account under its limit returns the validation error. The status code is what identifies
which check ran first. uvicorn has still taken the bytes off the socket — this stops the
work, not the transfer.

**One Redis client, one lifecycle.** `get_client()` in
[backend/app/core/redis.py](backend/app/core/redis.py) is now the only place a client is
built. Before it, the readiness probe built its own on every poll and closed it, which is
a real cost rather than a style point. Measured — `redis-cli info stats`, twenty
`GET /health/ready` requests, the counter read before and after:

| Probe implementation | Increase in `total_connections_received`, 20 probes |
|---|---|
| A client built and closed per probe | **23** |
| The process's shared client | **2** |

Redis carries rate limiting and the Celery broker. Caching and Pub/Sub arrive with the
phases that give them a consumer — S and R — and the refusal to cache the per-request
user/organization join, because ADR-013 requires deactivation to bite on the next request
rather than one TTL later, is recorded with its reasoning in ADR-022.

## Notifications

Notifications are the one resource here whose access rule is not a role scope. §3's matrix
gives `NOTIFICATION_LIST` to all four roles, so the guard on every route is uniform and
everything that narrows a query lives in one predicate: `user_id = the caller`. A
notification belonging to a colleague produces the **same 404** as an id nobody wrote
(ADR-009), and so does one belonging to another tenant. Neither reply names the tenant, the
recipient, or the delivery state.

```
GET  /api/v1/notifications                 ?unread_only=&limit=&offset=   newest first
GET  /api/v1/notifications/unread-count    {"unread": 3}
POST /api/v1/notifications/{id}/read       idempotent — re-marking does not move the time
POST /api/v1/notifications/read-all        {"marked_read": 3}
```

**The row is written in the request's transaction; only the email is queued.** An
assignment, a customer reply, or a resolution writes the notification inside the same commit
that makes the change, so a notification can never describe something that did not happen.
The slow, fallible, external part — SMTP — is what goes to Celery, and the task is handed an
id and nothing else. That ordering is load-bearing: the task's first act is to read the row,
so it must be enqueued *after* the commit. ADR-023 has the reasoning and the measurement.

**What notifies whom**, from §26's list of seven. Four have producers on the request path,
and Phase Q added two that have no request at all:

| Event | Recipient |
|---|---|
| Ticket assigned | the new assignee |
| Ticket reassigned | the new assignee (distinguished by `from_value`) |
| New customer reply | the assigned agent |
| Ticket resolved | every portal login of the ticket's customer |
| SLA warning (scheduled) | the assignee, if there is one, **and** every active manager |
| SLA breach (scheduled) | the same |

`CREATED`, `UNASSIGNED`, `PRIORITY_CHANGED`, `INTERNAL_NOTE_ADDED`, `ATTACHMENT_ADDED` and
`REOPENED` notify nobody, because §26 does not list them — stated explicitly rather than
left to omission, and pinned by a test. AI-analysis completions arrive with T–W, and
**manager mentions are deferred**: the phrase occurs exactly once in the specification, with
no syntax, no resolution rule, and no UI, so `NotificationType.MENTION` stays in the enum
unreachable.

The two scheduled rows are the ones a request cannot produce, and they are kept in a set of
their own (`SCHEDULED_EVENT_TYPES`) rather than folded in with the request path's — the
division says which code path sends them, not whether they are sent. There is also no actor
to filter out of their own alert, because a system alert has no actor: that is the one
branch of `notify_for_event` `notify_sla_alert` does not share. See the SLA section.

Authorship of a reply is read from `message.sender_type`, not from the caller's role. An
`AI_DRAFT` has no role and must never be mistaken for the customer writing in (§41).

### Four processes, and why

Nothing sends email unless a worker is running, and the tests deliberately do not start one.
The suite replaces the *enqueue* with a recorder rather than turning on `task_always_eager`,
because eager mode runs the task inline — inside the request that produced the notification
— and the task body would then start a second event loop inside the one already serving that
request. `asyncio` refuses, so eager mode would fail every request that produced a
notification rather than merely testing differently.

```bash
# 1. the API, from backend/
python -m uvicorn app.main:app --loop app.core.event_loop:loop_factory --port 8000

# 2. the worker, from backend/ — --pool=solo because fork is not available on Windows
#    The -Q list is not optional: a worker that does not name every routed queue consumes
#    nothing from the ones it omits, silently. See the SLA section.
celery -A app.workers.celery_app worker --loglevel=info --pool=solo -Q notifications,sla

# 3. beat, from backend/ — exactly one process, and it needs no API and no worker
celery -A app.workers.celery_app beat --loglevel=info -s /tmp/celerybeat-schedule

# 4. the walkthrough: drives the API, then reads the message back out of Mailpit's API
python scripts/phase_p_walkthrough.py    # notifications
python scripts/phase_q_walkthrough.py    # SLA: the clock, the sweep, and the alerts
```

Mailpit is the local mail server, on SMTP `1025` with an HTTP UI and API on `8025`. The
walkthrough uses the API half, so the proof that a message *left the process* is a `GET`
rather than a person looking at an inbox.

### At-least-once, and the column that narrows it

`task_acks_late=True` is what stops a worker killed mid-send from silently losing an email,
and its price is that a worker dying after sending but before acknowledging is handed the
same task again. `notifications.emailed_at` makes the task return early on a row that
already carries one, which narrows that from "any redelivery duplicates mail" to "a crash
between the send and the mark does". It is **narrowed, not closed** — closing it would need
the send and the mark in one atomic step, which SMTP is not part of.

Retries are for transient failures only: a connection refused or a socket timeout is worth
another attempt, a refused recipient is not. Backoff is in minutes (1, 2, 4, 8, 10,
jittered), and the first version of this was `retry_backoff=True` — one second, which is
about fifty seconds for all six attempts, and gives up while a restarting mail server is
still booting. A run against a closed port is what caught it; see ADR-023. A notification
whose retries are exhausted is **not** retried again: the row stays with `emailed_at IS
NULL`, which is the query a backlog sweep would use.

Two settings are not defaults and both matter: `task_serializer`/`accept_content` are
JSON-only, because a worker unpickling from a broker runs whatever the payload says, and
`worker_prefetch_multiplier=1`, because a task here can block for a full SMTP timeout.

## SLA

Every organization is seeded with §27's four policies at registration, in the same
transaction as the organization and its founding admin, so a new tenant's clock works from
its first ticket rather than being inert until somebody opens a settings screen. The
numbers are §27's sample configuration and the code says so: its closing paragraph asks
that they not be presented as industry standards, and they are all editable.

| Priority | First response | Resolution | Warning at |
|---|---|---|---|
| `low` | 24 h | 72 h | 80% |
| `medium` | 8 h | 24 h | 80% |
| `high` | 2 h | 8 h | 80% |
| `urgent` | 30 min | 4 h | 80% |

```
GET   /api/v1/sla/policies              all four, in LOW…URGENT order   SLA_VIEW
PATCH /api/v1/sla/policies/{priority}   a partial edit                  SLA_CONFIGURE (admin)
```

Reading the targets is `SLA_VIEW` — admin, manager, and agent all hold it, because an agent
working to a deadline needs to know what the deadline is. Setting them is `SLA_CONFIGURE`,
admin alone. Inactive policies are still listed, since a settings screen has to show a
target in order to offer switching it back on; the clock ignores them. A `PATCH` is
validated against the **merged** row, so raising the response target past an untouched
resolution target is a `422` with a sentence rather than a `CheckViolationError` at commit.
Every edit writes an `SLA_POLICY_UPDATED` audit row carrying the before and the after —
both, because "what was the policy when this ticket breached" is the question an incident
review asks.

**There is no `/sla/tickets/{id}`.** A ticket's position is a field on the ticket:

```json
"sla": {
  "response":   {"timer": "response", "state": "warning", "due_at": "…",
                 "remaining_seconds": 293, "stopped_at": null,
                 "warned_at": "…", "breached_at": null},
  "resolution": {"timer": "resolution", "state": "on_track", "due_at": "…",
                 "remaining_seconds": 14180, "stopped_at": null,
                 "warned_at": null, "breached_at": null},
  "policy":     {"id": "…", "priority": "urgent", "response_time_minutes": 30,
                 "resolution_time_minutes": 240, "warning_threshold_percent": 80,
                 "is_active": true}
}
```

`state` is one of `on_track`, `warning`, `breached`, `met`. `remaining_seconds` is signed
and relative to `stopped_at` when the timer has stopped — "resolved with two hours to
spare", "resolved forty minutes late" — and to now when it has not. A stopped timer that
landed late reads `breached` and not `met`: §28's compliance metric counts a late
resolution as a miss, and `stopped_at` is what tells a client the work did happen.

**The clock is derived on every read and stored nowhere.** Every input is already a column
— `created_at` starts both timers, `first_response_at` and `resolved_at` stop them, and the
priority's policy supplies the targets — and `resolve_position` is a pure function of those
facts. It does not read `status` at all: whether an overdue resolution is worth *alerting*
about is the sweep's judgement, and a clock that consulted status would be a clock with a
policy in it. The API and the sweep call the same function, so they cannot disagree. A
stored `sla_due_at` would be a second copy of a fact that follows from the columns above,
and it would go stale the moment a ticket was reprioritised. `alembic check` reporting no new
tables, columns, or indexes is the mechanical proof that nothing was stored — and the one
migration this phase does ship adds an enum value, which is not a place a position could hide.

**Every route that returns a ticket returns its clock**, not only the two read routes — a
`TicketRead` carries `"sla": null` when it is not decorated, which is the same payload a
portal caller gets, so a client rendering from a mutation response would watch the
countdown vanish on assign. All eight routes share one helper.

### Two clocks, four alerts, and a guard

`POST /tickets/{id}/messages` by staff sets `first_response_at`; a resolution sets
`resolved_at`, and reopening clears it. Neither is written by this phase — both were
already written by the right code, with the right semantics.

The sweep turns a state into an alert **once per timer per state**. Each of the four is
behind its own guard, so a ticket nobody touches for a week on URGENT produces one warning
and one breach for its response clock and one of each for its resolution clock, and is then
silent forever. The guard reads the ticket's own timeline: the alert record *is* a
`TicketEventType.SLA_WARNING` or `SLA_BREACHED` entry with `actor_user_id IS NULL`, which is
exactly what that nullable column was documented for. One query fetches the page's SLA
entries and serves both the judgement and the display, because the row the API reports as
`warned_at` / `breached_at` is the row the guard reads — so the two cannot disagree about
what has already been said. The entry records the deadline it fired against, so editing the
policy next month does not rewrite what the timeline says happened.

A **breach is a second alert, not a correction of the first**: §26 names only the warning,
and "you have 20 minutes" and "you are 40 minutes late" were both true when they were sent.
Nothing is retracted when the second goes out. A timer that was already past its deadline
the first time the sweep looked at it sends the breach and never the warning — "you have 20
minutes" arriving after the deadline would be worse than the miss it describes.

### The sweep, and the queue it runs on

```bash
# both, from backend/ — the API is not needed for beat
celery -A app.workers.celery_app worker --loglevel=info --pool=solo -Q notifications,sla
celery -A app.workers.celery_app beat   --loglevel=info -s /tmp/celerybeat-schedule
```

`beat` fires `check_sla_deadlines` every `SLA_SWEEP_INTERVAL_SECONDS`, which reads the
active organization ids and dispatches one `check_organization_sla` per tenant. Two tasks
rather than one loop over the fleet, so one tenant's backlog cannot delay every other
tenant's alerts and a failure is logged against the tenant that caused it. Each sweep
commits once: every timeline entry and every notification row for that tenant lands
together, so an alert can never be recorded with nobody notified — which would be
permanent, since the guard reads the timeline.

**The sweep runs on its own `sla` queue, and that makes `-Q` load-bearing.** With one queue
a missing `-Q` is invisible, because the default is the only queue that exists. With two, a
worker started without `-Q sla` **consumes nothing at all** while looking perfectly healthy:
it connects, reports ready, and leaves every SLA task in Redis forever, with no error, no
metric and no log line. That is not a prediction — a worker started with `-Q notifications`
alone sat for twenty seconds with four sweep tasks queued behind it (`LLEN sla` steady at 4),
having logged nothing beyond `ready`, and drained all four the moment the same worker was
started with `-Q notifications,sla`. Nothing in the application can notice, because from
Celery's point of view nothing is wrong — so
[tests/integration/test_celery_wiring.py](backend/tests/integration/test_celery_wiring.py)
parses `docker-compose.yml` and the `Makefile` and asserts the worker command names every
queue in `task_routes`. The two cannot drift.

**Exactly one beat process.** Two would double every sweep; the guard would stop the second
one from double-alerting, but the waste would be silent.

### What the portal sees

`TicketRead` is shared by all four roles, and §3 withholds `SLA_VIEW` from the customer, so
a portal caller's `"sla"` is `null` and its timeline hides the SLA entries. That null is
deliberately indistinguishable from "this priority has no active policy" — the second is a
fact about the tenant's configuration, and telling a customer which priorities their
provider has targets for is the disclosure the null exists to prevent.

## Security posture

Implemented in Phase C:

- Secrets have no defaults and are validated at startup; `JWT_SECRET` under 32
  characters is rejected outright.
- `.gitignore` excludes all `.env` files except the template.
- CORS uses an explicit origin allowlist, never a wildcard.
- OpenAPI docs are disabled when `ENVIRONMENT=production`.
- Health endpoints disclose booleans only — no hostnames, no connection strings.
  A test asserts this.
- The production image runs as a non-root user and excludes tests and build
  artifacts.
- Ruff runs `bandit` security rules across the codebase.

Implemented in Phases F–H:

- **Passwords** hashed with Argon2id, per-hash salt, constant-time verification, and a
  dummy verify on the unknown-email path so response timing does not disclose whether
  an account exists.
- **Access tokens** algorithm-pinned on decode, so the `alg: none` forgery is refused.
  Expiry, wrong-signature, tampering, and missing-claim cases are each tested.
- **Refresh tokens** stored only as SHA-256 hashes, rotated on every use, with reuse
  detection revoking every live session for the affected user.
- **Authorization** centralized in one mapping, with mechanical tests that fail on a
  scattered role comparison or an unprotected route.
- **Tenant isolation** enforced at three layers plus a token/row cross-check, with a
  dedicated `pytest -m security` suite.
- **Rate limiting** on login, registration, and file upload, returning `429 RATE_LIMITED`
  with `Retry-After`. Login and registration are keyed on the client address; upload is
  keyed on the user. See [Rate limiting](#rate-limiting).
- **No tokens or passwords in logs.** Every log statement passes identifiers — user
  and organization ids — and never a credential. This is enforced rather than
  reviewed: [tests/security/test_log_hygiene.py](backend/tests/security/test_log_hygiene.py)
  records everything the application logs across a full session and asserts no
  password, access token, refresh token, authorization header, or password hash
  appears anywhere in it. It carries a positive control, so a recorder that captured
  nothing fails rather than passing vacuously.

Designed and documented, enforced in later phases: upload validation, and treating AI
output as untrusted until schema-validated. See
[docs/requirements.md](docs/requirements.md) §7.

Implemented in Phases I–K:

- **Row scope** applied where the query is built, never at the route, and an
  unresolvable scope fails closed rather than open (ADR-015). Asserted directly in
  [tests/security/test_row_scopes.py](backend/tests/security/test_row_scopes.py),
  including the case that cannot be reached over HTTP.
- **Cross-tenant access to customers, tickets, and messages** returns `404`, with the
  body compared against a genuinely absent record's — and every refused write followed
  by proof that the target was untouched.
- **Every mutating ticket route** is swept one by one for cross-tenant reach, because
  the guard that gets missed is never the one somebody thought to test.
- **A client cannot name the tenant, the customer, or the actor.** `organization_id` is
  not a field on any create schema, and a portal caller's `customer_id` comes from their
  user row — naming someone else's is a `422`, and a test forges it.
- **Customer search escapes `LIKE` wildcards.** The value is parameterized, so there is
  no injection, but `%` and `_` in a search term are still wildcards: a customer
  searching for `50%` would otherwise match every record.
- **Ticket creation is serialized per tenant** by a transaction-scoped advisory lock, so
  a duplicate number is impossible rather than merely retried (ADR-016).
- **The log-hygiene test now covers the new services.** It drives a customer, a ticket
  through the whole lifecycle, a reply, and an internal note, and asserts no password,
  token, or hash appears in any of it — with the same positive control.

Two trade-offs are deliberate and recorded in ADR-014: the login limiter **fails open**
when Redis is unreachable (it is an abuse control, not an authentication control, and
failing closed would turn a Redis blip into a total login outage), and it is keyed on
the **client address rather than the account** — verified against a running server, a
legitimate login from the same address is throttled alongside an attacker's. The upload
limiter, arriving later, declines to inherit the second of those and keys on the user
instead; see [Rate limiting](#rate-limiting).

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| A | Requirements, permission matrix | ✅ |
| B | Architecture and flows | ✅ |
| C | Repository, tooling, Docker, CI | ✅ |
| D | Domain models, relationships, indexes | ✅ |
| E | Alembic migrations | ✅ |
| F–H | Auth, RBAC, multi-tenancy + security tests | ✅ |
| I–K | Customers, tickets, messages | ✅ |
| L–N | Attachments, audit logging, search | ✅ |
| O | Redis: shared client, rate-limit consolidation | ✅ |
| P | Celery, notifications, email delivery | ✅ |
| Q | SLA monitoring, beat | ✅ |
| R–S | WebSockets, analytics | next |
| T–W | AI foundation, analysis, summaries, drafts | |
| X | Knowledge base and RAG | |
| Y–Z | Hardening, deployment | |

## Known limitations

- Four migrations exist: the baseline, Phase N's message full-text index, Phase P's
  `notifications.emailed_at`, and Phase Q's `notification_type` enum value. The baseline is
  still the first revision, so there is no upgrade path from an older schema.
- The test suite builds its schema with `Base.metadata.create_all` rather than by
  applying migrations. That keeps tests fast, and the drift check in
  [tests/integration/test_migrations.py](backend/tests/integration/test_migrations.py)
  is what stops the two from diverging.
- The embedding provider is deliberately undecided until Phase X (ADR-008).
- Redis carries rate limiting and the Celery broker. Caching and Pub/Sub are assigned to
  the phases that give them a consumer — S and R — rather than built ahead of one
  (ADR-022).
- **A "ticket resolved" notification reaches the customer's *portal login*, so a customer
  record with no login gets no notification at all** — no row is written, rather than a row
  addressed to the agent who resolved it. `notifications.user_id` is NOT NULL and §26
  describes no second recipient model (ADR-023).
- **A customer can have more than one portal login**, and a resolution then notifies all of
  them. Nothing forbids the second account; the alternative was picking one arbitrarily.
- **A notification whose five email retries are exhausted is not retried again.** The row
  keeps `emailed_at IS NULL`, which is the query a backlog sweep would use, but that sweep
  is not written yet.
- **Manager mentions are specified but unbuilt.** §26 lists them once, with no syntax and no
  resolution rule. `NotificationType.MENTION` is in the enum and unreachable.
- **No WebSocket push.** The notification is persisted so that Phase R has something to
  push, but clients poll today.
- **SLA timers run on wall-clock UTC, with no business calendars and no pause.** §27's
  figures are bare durations, and the spec mentions business hours, calendars, or timezones
  nowhere. A ticket raised at 23:50 on Friday against a 2-hour target warns at 00:10 on
  Saturday. `WAITING_FOR_CUSTOMER` does **not** stop the resolution clock: pausing means
  storing elapsed pause time, which is a schema change for a rule the spec does not state.
- **Four policies are seeded rather than configured.** A new organization gets §27's sample
  numbers, which is a product decision the spec does not make — §27 gives values, which
  reads as defaults, and "an admin must configure the SLA before anything works" is not a
  reasonable first run. They are editable from the first request.
- **A lost SLA email is not retried past the five attempts.** The `ticket_events` row is the
  record and the notification is its delivery; a missing email narrows the alert but does
  not erase it, and the timeline entry is still there when the agent opens the ticket. This
  is different from a notification whose subject has no other record, which is why the
  policy is stated rather than assumed.
- **The SLA alert is not pushed.** Like every other notification it is persisted for Phase R
  and polled today, so a warning's latency is the poll interval plus the sweep interval.
- **One beat process, and two would double every sweep.** The timeline guard stops the
  duplicate from double-alerting, so the failure is invisible rather than loud — the only
  thing standing between it and silence is that beat is a single service in the compose file.
- **The sweep is bounded at `SLA_SWEEP_BATCH_SIZE` tickets per priority per tenant**, served
  oldest first. A tenant with more overdue tickets than that has the remainder picked up by
  the next sweep, so the bound delays alerts rather than dropping them — but on a backlog
  large enough to matter it is the *newest* tickets at that priority whose warnings arrive
  late, and nothing reports that they did.
- The deployment target is undecided; nothing in the architecture depends on a
  specific cloud.
- On Windows, the API must be started with
  `--loop app.core.event_loop:loop_factory` (`make api` includes it). See ADR-011.
- **Login cannot disambiguate two accounts that share an email *and* a password.**
  Email is unique per organization, so the credential is resolved without a tenant in
  the request. Distinct passwords resolve unambiguously; identical ones return one of
  the two, deterministically. The fix is to take the tenant from the request host (a
  subdomain per organization), which a JSON API alone cannot express. Asserted rather
  than hidden — see
  [tests/security/test_tenant_isolation.py](backend/tests/security/test_tenant_isolation.py).
- **Rate limiting is per client address, not per account**, so users behind a shared
  NAT share one bucket. Fixing it needs a trusted-proxy list and a hybrid key, which
  needs a deployment target. See ADR-014.
- **No "log out everywhere" endpoint.** `/auth/logout` revokes one session; whole-family
  revocation exists for reuse detection. ADR-003 anticipates an all-sessions endpoint
  if it is wanted.
- **No email verification or password reset** yet — both need the outbound mail path
  (Mailpit is already in the stack for it).
- **Ticket lists return every matching row for an admin.** Pagination caps the page, not
  the total, and `GET /tickets` has no `count()`. This matches `/users` and is the
  documented behaviour rather than an oversight — a total can be added later as a
  compatible change.
- **Search over the four arms does not use the full-text indexes**, and degrades linearly
  with an organization's ticket count — a term matching nothing costs a full pass over
  that organization's tickets (103 ms for 5,000). The individual arms are index-driven and
  the reachable fix is to union them rather than OR them, which is a change to the query
  shape and is deliberately not smuggled in with the feature. The measurement is in
  [the section above](#what-the-index-measurement-actually-showed).
- **Attachment objects are orphaned when a ticket is deleted.** Rows are cascade-deleted
  with the ticket, and nothing removes the objects from the bucket — deleting stored
  objects needs a lifecycle policy or a sweeper, and neither belongs in a request. There
  is also **no attachment delete route**: the permission matrix has no `ATTACHMENT_DELETE`
  row, so none was invented.
- **`attachments.message_id` is `SET NULL` when a message is deleted**, so an attachment
  outlives the message it arrived with — and an attachment on an internal note would lose
  its internal marking and become ticket-level. There is no message-delete route, so the
  path is unreachable today; the day one is added is the day this needs revisiting. It is
  a comment in the code rather than a silent gap.
- **No virus scanning, no thumbnails, no OCR.** Files are validated for type and size and
  stored; nothing inspects their contents beyond the leading signature.
- **Message hits in search are not relevance-ranked.** The message arm is a boolean
  predicate, so it decides which tickets match and not what order they come back in;
  results are ordered by the `sort` parameter like any other list.
- **Ticket and customer lists have no total count.** Pagination caps the page, not the
  total. A total is a compatible addition later, and `GET /audit-logs` follows the same
  convention.
- **Audit rows are never written for customer or message writes.** `AuditAction` is a
  PostgreSQL enum with no member for either, and both are already attributable through
  `ticket_events`.
- **The audit log is append-only by construction, not by privilege.** No route and no
  repository method updates or deletes a row, and the model has no `updated_at` — but the
  application's database role can still do both directly.
- **Ticket creation serializes within a tenant.** One advisory lock per organization, so
  a support desk's write path holds the lock only for the length of one short
  transaction. The alternative — a retry loop on a unique-violation — was worse; see
  ADR-016.
- **A reopen discards the assignment**, so the ticket goes back on the queue rather than
  to whoever had it. That is a deliberate reading of `OPEN` as "nobody owns this", and
  the history is not lost: it is in `ticket_events`.
- **The frontend has no screens for any of this.** Every phase since C has been
  backend-only; the routes are exercised by 930 tests, and the SPA still shows the Phase
  C scaffolding.

