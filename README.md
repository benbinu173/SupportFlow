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
| `REDIS_URL` | Cache and pub/sub (required) |
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

Users are the only tenant-owned resource built so far. Tickets, customers, messages,
knowledge articles, attachments, and audit logs arrive in Phases I–N and follow the
identical repository pattern. See [docs/data-model.md](docs/data-model.md).

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
- **Rate limiting** on login and registration, returning `429 RATE_LIMITED` with
  `Retry-After`.
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

Two trade-offs are deliberate and recorded in ADR-014: the rate limiter **fails open**
when Redis is unreachable (it is an abuse control, not an authentication control, and
failing closed would turn a Redis blip into a total login outage), and it is keyed on
the **client address rather than the account** — verified against a running server, a
legitimate login from the same address is throttled alongside an attacker's.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| A | Requirements, permission matrix | ✅ |
| B | Architecture and flows | ✅ |
| C | Repository, tooling, Docker, CI | ✅ |
| D | Domain models, relationships, indexes | ✅ |
| E | Alembic migrations | ✅ |
| F–H | Auth, RBAC, multi-tenancy + security tests | ✅ |
| I–K | Customers, tickets, messages | next |
| L–N | Attachments, audit logging, search | |
| O–Q | Redis, Celery, SLA | |
| R–S | WebSockets, analytics | |
| T–W | AI foundation, analysis, summaries, drafts | |
| X | Knowledge base and RAG | |
| Y–Z | Hardening, deployment | |

## Known limitations

- One migration exists: the baseline. There is no upgrade path *from* an older schema
  yet, because the baseline is the first revision.
- The test suite builds its schema with `Base.metadata.create_all` rather than by
  applying migrations. That keeps tests fast, and the drift check in
  [tests/integration/test_migrations.py](backend/tests/integration/test_migrations.py)
  is what stops the two from diverging.
- The embedding provider is deliberately undecided until Phase X (ADR-008).
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

