# Architecture Decisions

Records where the implementation departs from
`SupportFlow_Master_Build_Specification.txt`, or where the spec left a choice open
and a decision was needed. Newest last. Each entry states the decision, why, and what
it costs.

---

## ADR-001 — Argon2id via `pwdlib` instead of `passlib`

**Status:** accepted · Phase C

**Context.** The spec requires secure password hashing (§4.8, §10) without naming a
library. `passlib` is the conventional choice in FastAPI tutorials.

**Decision.** Use `pwdlib` with Argon2id.

**Why.** `passlib` is broken against current `bcrypt`. Verified on this machine:
`CryptContext(schemes=["bcrypt"])` raises during backend detection —

```
ValueError: password cannot be longer than 72 bytes, truncate manually if necessary
```

`passlib` probes for a legacy wrap bug using a >72-byte input, which `bcrypt` 5.0 now
rejects outright instead of silently truncating. `passlib`'s last release was 2020, so
this will not be fixed upstream. `pwdlib` was verified working in the same
environment, produces `$argon2id$` hashes, and is maintained by the fastapi-users
author. Argon2id is memory-hard, has no 72-byte input limit, and is the current
recommendation for new applications.

**Cost.** Less tutorial material references `pwdlib`. Argon2id verification uses more
memory per call than bcrypt, which matters only under login-heavy load and is
mitigated by rate limiting.

---

## ADR-002 — `PyJWT` instead of `python-jose`

**Status:** accepted · Phase C

**Context.** The spec requires JWT access tokens without naming a library.

**Decision.** Use `PyJWT`.

**Why.** `python-jose` installs on Python 3.14 but is effectively unmaintained, with
open advisories and no meaningful release activity. `PyJWT` is actively maintained,
is what the FastAPI documentation now uses, and was verified encoding and decoding
correctly here. It also warns on undersized HMAC keys, which is a useful guard against
a weak `JWT_SECRET`.

**Cost.** `PyJWT` covers JWT/JWS only, not the wider JOSE suite. This project needs
nothing beyond signed JWTs.

---

## ADR-003 — Opaque refresh tokens, not JWTs

**Status:** accepted · Phase C

**Context.** The spec requires refresh tokens to be revocable (§10).

**Decision.** Access tokens are JWTs. Refresh tokens are high-entropy random strings,
stored hashed in Postgres, rotated on every use, with family revocation on reuse of a
consumed token.

**Why.** A stateless JWT cannot be revoked before expiry without a server-side
blocklist — at which point it carries the storage cost of a session with none of the
benefits. Storing refresh tokens hashed makes revocation a row update, supports
"log out everywhere," and turns token reuse into a detectable theft signal. Hashing at
rest means a database read alone does not yield usable credentials.

**Cost.** One database round trip per refresh. Negligible, since refreshes are rare
relative to API calls.

---

## ADR-004 — Tests at `backend/tests/`, not `backend/app/tests/`

**Status:** accepted · Phase C

**Context.** The spec's structure (§37) places `tests/` inside `app/`.

**Decision.** Put tests at `backend/tests/`, preserving the spec's
`unit/ integration/ security/ api/` subdivision.

**Why.** `app/` is the installable package. Tests nested inside it ship to production
images, get imported by package scanners, and blur the line between shipped code and
verification code. Keeping them as a sibling is the standard Python layout and lets
the production Dockerfile exclude them cleanly.

**Cost.** Divergence from the spec's literal tree; the subdivision it asked for is
kept intact.

---

## ADR-005 — Python 3.14 with pinned dependencies

**Status:** accepted · Phase C

**Context.** Python 3.14.7 is installed locally. New minor versions often lack
compiled wheels for database and crypto packages, which on Windows means a source
build and a toolchain requirement.

**Decision.** Target Python 3.14 in both local development and Docker.

**Why.** Verified by resolving the full dependency set with `--only-binary=:all:`,
which succeeded: FastAPI 0.141.1, SQLAlchemy 2.0.52, psycopg 3.3.5 (binary),
Celery 5.6.3, pgvector 0.5.0, pytest 9.1.1. No source builds required, so the
usual reason to pin back a version does not apply here.

**Cost.** Some libraries are newer than most published examples. Dependencies are
pinned so CI, Docker, and local development resolve identically.

---

## ADR-006 — `pgvector/pgvector:pg17` image

**Status:** accepted · Phase C

**Context.** The spec requires the pgvector extension (§5). The official `postgres`
image does not include it.

**Decision.** Use `pgvector/pgvector:pg17` for local development and CI.

**Why.** Verified locally: extension version 0.8.6 on PostgreSQL 17.11, with an HNSW
cosine index building successfully on `vector(1536)`. Avoids a custom Dockerfile or a
fragile init script.

**Cost.** Ties local development to a community image rather than the official one.
In production a managed Postgres with pgvector enabled substitutes directly.

---

## ADR-007 — Hybrid local development: Docker for infrastructure, native app

**Status:** accepted · Phase C

**Context.** The spec asks for a Compose setup covering frontend, backend, Postgres,
Redis, and Celery (§48). Two Windows-specific problems complicate running the app
tier in containers day to day.

**Decision.** By default, Compose runs Postgres, Redis, MinIO, and Mailpit; the API,
Celery worker, and Vite dev server run natively on the host. A full-stack Compose
profile covers parity checks and CI.

**Why.** Celery's prefork pool does not work on Windows, so a native worker needs
`--pool=solo` regardless. Bind-mount file watching from Windows into Linux containers
is slow and unreliable, which degrades both uvicorn and Vite reloads. Running stateful
infrastructure in containers captures the real benefit — reproducible Postgres with
pgvector, no local installs — while native app processes keep reload fast and
debugger attachment simple.

**Cost.** Two supported paths instead of one. Mitigated by keeping the full-stack
profile in CI so it cannot silently rot.

---

## ADR-008 — Anthropic Claude as the first provider, with a separate embedding provider

**Status:** accepted · Phase C

**Context.** The spec requires a provider abstraction plus an embedding model (§5,
§17) and forbids shipping fake AI results (§60).

**Decision.** Implement `AIProvider` with Claude behind it for classification,
sentiment, summarization, and drafting, using tool-use for structured output.
Embeddings come from a separate provider behind the same interface, since Anthropic
publishes no embedding model. A deterministic fake provider exists for tests only.

**Why.** Splitting generation from embedding is exactly the pressure that justifies
the abstraction — the interface has to survive two vendors from day one rather than
being a single-vendor wrapper. Tool-use gives schema-constrained output, which pairs
naturally with the Pydantic validation the spec demands. The fake provider keeps AI
tests fast, free, and deterministic without appearing in production paths.

**Cost.** Two API credentials and two failure modes. The embedding provider decision
is deferred to Phase X, when RAG is actually built.

---

## ADR-009 — Cross-tenant reads return 404, not 403

**Status:** accepted · Phase C

**Context.** The spec defines a `TENANT_ACCESS_DENIED` error code (§42) and demands
strict isolation (§11).

**Decision.** Requesting a record belonging to another organization returns `404` with
the resource's not-found code. `TENANT_ACCESS_DENIED` is reserved for logging and for
cases where tenant mismatch is itself the reportable condition.

**Why.** A `403` distinguishes "exists elsewhere" from "does not exist," letting an
attacker enumerate valid IDs across the platform. `404` leaks nothing. The event is
still logged server-side at a level that surfaces probing.

**Cost.** Slightly less informative for a legitimate client that has genuinely gone
looking in the wrong organization — an unusual case, and worth the tradeoff.

---

## ADR-010 — Premium agency visual language, calibrated by screen density

**Status:** accepted · Phase C (applied from Phase S onward)

**Context.** The spec requires the application to "look like a real SaaS product," not
a tutorial (§39), and names the ticket detail screen as the strongest screen in the
application (§40). The `high-end-visual-design` agent skill is installed and was
chosen as the design direction.

**Decision.** Adopt the skill's visual language — nested "double-bezel" container
architecture, exaggerated squircle radii, custom cubic-bezier motion, diffused ambient
shadows over hard drop shadows, premium typography, ultra-light icon strokes — across
the application.

Calibrate two of its rules by screen type rather than applying them uniformly:

| Rule | Marketing / auth screens | Dense product screens |
|---|---|---|
| Section padding `py-24`–`py-40` | applied | reduced; density is the goal |
| Scroll-driven entry animation | applied | mount transitions only, no scroll choreography |

Everything else — bezels, radii, motion curves, shadow treatment, typography, icon
weight, and every performance guardrail — applies everywhere without exception.

**Why.** The skill's own framing is agency and landing-page work. Its craft
vocabulary transfers cleanly to product UI; two of its *spatial* rules do not. An
agent triaging a queue needs to compare many tickets in one viewport, so `py-40`
between sections would push a 20-row list into three scroll-lengths and make the tool
slower to use. Scroll-triggered reveals are worse still on a work surface: an agent
scanning a list would watch rows fade in repeatedly, adding latency to every glance.

Keeping the rest is not a compromise. Depth, concentric radii, spring-physics motion,
and restrained iconography are what separate Linear from a Bootstrap dashboard, and
they cost nothing in density. The skill names Linear explicitly as its reference
point, and Linear is a dense product UI — evidence the vocabulary itself is
compatible, and that only the marketing-page spacing needs adjusting.

The skill's performance guardrails are adopted verbatim, since they are correct
regardless of aesthetic: animate only `transform` and `opacity`, `backdrop-blur` on
fixed and sticky elements only, `IntersectionObserver` rather than scroll listeners,
noise overlays on fixed `pointer-events-none` layers, and systemic z-index tiers.

**Verified available** (npm, at time of writing): `geist` 1.7.2 and
`@fontsource-variable/plus-jakarta-sans` 5.3.0 for typography;
`@phosphor-icons/react` 2.1.10 for light-stroke icons; `motion` 13.2.0 for
`whileInView` and spring transitions. The skill bans Inter and thick-stroke Lucide,
so the default choices are deliberately avoided.

**Accessibility.** The skill is silent on `prefers-reduced-motion`; the spec requires
accessible interfaces (§39). All motion must therefore respect the media query, and
the low-contrast hairlines the aesthetic favours must still clear WCAG AA. Vercel's
`web-design-guidelines` skill runs as an audit gate over each screen to enforce this.

**Cost.** More implementation effort per screen than a component library would need,
and two spacing scales to keep straight. The density calibration is a judgement call —
if the dense screens end up feeling visually disconnected from the marketing surfaces,
this decision is the thing to revisit.

---

## ADR-011 — An explicit event loop factory, shared by the app and the tests

**Status:** accepted · Phase D

**Context.** The stack uses psycopg 3 in async mode under FastAPI. On Windows,
`uvicorn app.main:app` raises on the first query:

```
psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode.
```

psycopg's async mode drives sockets through `add_reader`/`add_writer`, which the
Proactor loop does not implement for sockets. Verified on this machine: uvicorn selects
`ProactorEventLoop` when it is *not* spawning a subprocess, so `--reload` and
`--workers N` silently work while plain `uvicorn` fails. The failure appears at first
query, not at startup, so it presents as a broken endpoint rather than a boot error.

**Decision.** Define `app/core/event_loop.py` exposing a zero-argument `loop_factory`,
and pass it to uvicorn via `--loop app.core.event_loop:loop_factory`. The same factory
is supplied to pytest (through `pytest_asyncio_loop_factories`) and to Starlette's
`TestClient` (through `backend_options`).

**Why.** The conventional fix, `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())`,
is deprecated in Python 3.14 and slated for removal in 3.16 — and ADR-005 pins the
project to 3.14. A loop factory is the documented replacement and is accepted by both
`asyncio.Runner` and uvicorn, so one function serves every entrypoint.

Sharing it with the tests is the part that matters most. `TestClient` runs the app on
its own loop in a worker thread; left alone that loop is a Proactor loop, so routing
tests passed while every endpoint touching Postgres reported itself unavailable. A
green suite that cannot reach the database is worse than a red one, and the readiness
tests are what caught it.

**Cost.** Windows-only code path. It is a no-op on Linux, where the default selector
loop already supports the required calls, and the container is Linux — so this must
never become load-bearing. The `--loop` flag is required in the local run target and in
any Windows script that starts the API; omitting it reintroduces the failure with no
warning at startup. `make api` carries a comment for that reason.

---

## ADR-012 — Migrations run on a synchronous engine, and the baseline owns its extensions

**Status:** accepted · Phase E

**Context.** Three questions came up setting up Alembic that the specification does not
answer, and each had a defensible answer in both directions.

The application is async throughout, so the obvious move is an async Alembic engine
using `AsyncConnection.run_sync`. But ADR-011 exists because psycopg's async mode
cannot drive Windows' ProactorEventLoop — an async migration engine would drag that
workaround into every `alembic` invocation, for a batch job where latency is
irrelevant.

Second, the schema depends on two extensions (`vector` for the embedding column,
`pg_trgm` for the customer search indexes), and the docker entrypoint already installs
them. Whether the migration should also create them is a question of who owns the
database's initial state.

Third, `alembic.ini` is committed, and the database URL contains a password.

**Decision.**

- Alembic uses a **sync** engine. `alembic/env.py` calls `create_engine` on
  `settings.sqlalchemy_dsn`, which names the `psycopg` dialect explicitly so the app
  and its migrations cannot diverge onto different drivers.
- The baseline migration issues `CREATE EXTENSION IF NOT EXISTS vector` and
  `pg_trgm` itself, before any DDL that needs them. `uuid-ossp` is deliberately not
  created: `gen_random_uuid()` has been core since PostgreSQL 13.
- `alembic.ini` contains no URL. `env.py` reads configuration from
  `app.core.config`; a programmatic caller can override it through
  `config.attributes["db_url"]`, which is Alembic's supported channel for that and is
  what the migration tests use.
- The environment also passes `disable_existing_loggers=False` to `fileConfig`, so an
  in-process run does not switch off pytest's loggers mid-session.

**Why.** The sync engine confines ADR-011's Windows workaround to the app, where it is
already tested, rather than spreading it to a second entrypoint. Verified by running
migrations against a database created without either extension: both were installed by
the migration, and the vector column and trigram indexes were created successfully.
That test is now permanent —
`test_upgrade_installs_the_extensions_the_schema_needs` runs against a scratch database
the fixture deliberately creates extension-free, so a database that already had them
cannot hide a regression.

Keeping the DSN out of `alembic.ini` is a §54 requirement rather than a preference. It
also removes a real failure mode: a URL duplicated in two files drifts, and the copy in
`alembic.ini` is the one that would be forgotten.

**Cost.** Two engines now exist in the codebase, sync and async, which is one more
thing to explain. The extension decision makes the migration non-portable to a
database where the extensions cannot be installed — acceptable, since neither the
schema nor pgvector's operator classes work without them. `config.attributes` is an
attribute of an Alembic object rather than a typed API, so the override is
convention rather than something the type checker enforces.

**A defect this phase surfaced.** `alembic check` — added here as a drift gate — failed
on the first run. The model declared `ix_tickets_fts` as
`to_tsvector('english', subject || ' ' || description)`; PostgreSQL stores
`to_tsvector('english'::regconfig, (subject::text || ' '::text) || description)`.
Autogenerate compares index expressions as text, so it reported the index as changed
and would have emitted a drop-and-recreate in every future migration. The model was
rewritten to the stored form. The drift check now asserts an empty diff, and was
verified to fail by reintroducing the old expression — a test that cannot fail is not
a test.

---

## ADR-013 — A capability is a permission; "own" and "assigned" are scopes

**Status:** accepted · Phase G

**Context.** `docs/requirements.md` §3 has 38 rows, and several carry a qualifier:
ticket detail is `assigned` for an agent and `own` for a customer; message edit is
`own`. The spec's §51 requirement is "centralized RBAC — no scattered role
comparisons", and §54 requires authorization on every protected resource.

The obvious transcription is one permission per row *including* the qualifier —
`TICKET_VIEW_ASSIGNED`, `TICKET_VIEW_OWN`, `TICKET_VIEW_ORGANIZATION`. That reads
faithfully but it conflates two different questions: *may this role do this at all*,
and *which rows may they do it to*.

**Decision.** `Permission` (a `StrEnum`) has one member per matrix row with the
qualifier dropped. `ROLE_PERMISSIONS: Mapping[UserRole, frozenset[Permission]]` is the
single source of truth, transcribed row by row from §3. Row visibility is a separate,
centrally-defined mapping from role to `RowScope`:

```python
TICKET_SCOPE_BY_ROLE     = {ADMIN: ORGANIZATION, MANAGER: ORGANIZATION, AGENT: ASSIGNED, CUSTOMER: OWN}
MESSAGE_SCOPE_BY_ROLE    = {ADMIN: ORGANIZATION, MANAGER: ORGANIZATION, AGENT: ASSIGNED, CUSTOMER: OWN}
ATTACHMENT_SCOPE_BY_ROLE = {ADMIN: ORGANIZATION, MANAGER: ORGANIZATION, AGENT: ASSIGNED, CUSTOMER: OWN}
```

They are separate names rather than one map because they will diverge — an attachment
on an internal note is not visible to the customer who owns the ticket — and a test
asserts the three currently agree, so a deliberate divergence has to be made
deliberately.

Routes declare capabilities, never scopes:
`dependencies=[Depends(require_permission(Permission.TICKET_ASSIGN))]`. Repositories
take a `TenantContext` at construction and apply the scope to the query they build.

**Why.** The two questions are answerable in different places. "May an agent edit
tickets" is a property of the route and can be decided before the request body is
parsed. "Which ticket" is a property of the query and cannot be decided anywhere except
where the `WHERE` clause is written — a route-level check has no row to examine yet.

Collapsing them would also break the testability of the matrix. Keeping
`ROLE_PERMISSIONS` 1:1 with the documented table means
[tests/unit/test_permissions.py](backend/tests/unit/test_permissions.py) can transcribe
§3 *independently* and assert the two agree role by role. A permission list that
multiplied qualified rows could not be compared against the document at all, only
against itself.

The centralization requirement is enforced mechanically rather than by review:
`test_permissions.py` scans every module outside `app/core/permissions.py` for
`UserRole.ADMIN`-style references and for `role == "admin"`-style string comparisons,
with a short, justified allowlist (the last-admin invariant in two places, and the
registration path that assigns `ADMIN` to a new organization). A new scattered role
check fails the suite rather than passing review.

**Cost.** Two concepts to learn instead of one, and a repository author has to
remember to apply the scope — forgetting it fails *open* (the caller sees the whole
organization) rather than closed. That is the main risk this design carries, and the
mitigation is that `TenantScopedRepository` applies the organization filter in its
constructor, so the only thing a subclass can get wrong is narrowing further than the
scope requires.

**Related decision, in the same phase: the database is authoritative, not the token.**
The JWT carries `sub`, `org`, and `role`, and all three are *checked against the row*
on every request — the row wins. A stateless token that carried authority would keep
asserting a revoked role until it expired, so a demotion or deactivation would take up
to `ACCESS_TOKEN_EXPIRE_MINUTES` to bite. Reading the role from the database makes both
immediate. The claims are retained for cross-checking (a token whose `org` does not
match the user's current `organization_id` is refused with `TENANT_ACCESS_DENIED`, not
silently honoured) and for self-description.

The cost of that choice is one database round trip per request, which the joined load
of the organization's status already required. The test that pins it end to end is
`test_a_promotion_takes_effect_on_the_promoted_user_s_next_request` — the promoted
user's *existing* access token immediately grants more, which is only possible because
the token is not the authority.

---

## ADR-014 — The refresh cookie is the CSRF control; the rate limiter fails open

**Status:** accepted · Phase F

**Context.** Two decisions in this phase are security trade-offs where both directions
are defensible, and the reasoning is worth more than the outcome.

**The refresh token's storage.** It must survive a page reload, so it cannot live only
in memory. `localStorage` is readable by any script on the origin, so a single XSS
exfiltration is a long-lived session theft. A cookie is not readable by script if it is
`HttpOnly`, but cookies are sent automatically, which is what makes CSRF possible.

**The rate limiter's failure mode.** Login and registration are limited by Redis
fixed-window counters. Redis is a separate process and can be down while the API is up.
The limiter can either refuse the request (fail closed) or allow it (fail open).

**Decision.**

- The refresh token is set as a cookie: `HttpOnly`, `SameSite=Lax`, `Secure` outside
  development, and `Path=/api/v1/auth` so it is sent to the auth endpoints and nowhere
  else. The access token is returned in the response body and held in memory by the
  client. **No refresh token ever appears in a response body.**
- The rate limiter logs a warning and **allows** the request when Redis is unreachable.

**Why.** `SameSite=Lax` is what makes the cookie safe without a token-pair dance: a
cross-site POST does not carry it, so the refresh and logout endpoints cannot be driven
by a page the user did not intend to visit. This is what architecture §4 means by "the
refresh endpoint carries CSRF protection" — it is a property of the cookie attribute,
and the ADR records it so it is a decision rather than an accident. It also keeps local
development working: `localhost:5173 → localhost:8000` is same-*site* (the port is not
part of a site), so Lax cookies are sent.

`Path=/api/v1/auth` is defence in depth rather than a CSRF control: it means a bug in
an unrelated endpoint cannot receive the refresh token, and the cookie does not travel
with every API call.

Failing open is a judgement about which outage is worse. Rate limiting is an abuse
control, not an authentication control. Failing closed converts a Redis blip into a
total login outage for every user of every tenant — an availability failure caused by a
dependency that was only ever meant to slow an attacker down. Failing open means a few
minutes of unthrottled attempts during a Redis outage, against Argon2id verification
that is itself deliberately expensive.

**Fail-open is only defensible while it is audible**, so the limiter logs
`rate_limit_unavailable` at warning level with the key and the error, and
`test_the_open_failure_is_logged` asserts it. A limiter that silently stopped
protecting the login endpoint would be worse than no limiter, because the metrics would
still look healthy.

**Cost.** Three, all accepted knowingly:

1. **Lax is not Strict.** `SameSite=Strict` would break the case where a user follows a
   link into the app from elsewhere and arrives without their session. Lax is the
   standard compromise, and it still blocks the cross-site POST that CSRF needs.
2. **The limiter is keyed on the client address, not the account** (`request.client.host`,
   deliberately not `X-Forwarded-For` — see the docstring on `client_ip`). This was
   verified against the running server: after the limit is reached, a *legitimate* login
   from the same address is refused too, because the counter is per-address rather than
   per-credential. That is the correct trade-off for credential stuffing, and it is
   wrong for a shared NAT — a whole office would share one bucket. The real fix is a
   trusted-proxy list plus a hybrid key, which needs a deployment target this project
   does not have yet.
3. **Fail-open is a real hole during an outage**, bounded by the outage's length and
   visible in the logs.

---

## ADR-015 — Row scope is applied in the repository, and an unresolvable scope fails closed

**Status:** accepted · Phase I–K

**Context.** ADR-013 split authorization into a capability (decided at the route) and a
row scope (not decidable there, because there is no row yet) and left the scope
mechanism built but unconsumed. Phases I–K are the first phase with rows to narrow, and
they are where a scope that fails *open* leaks one customer's support conversation to
another.

Three questions had to be answered, and each has a plausible wrong answer that looks
right.

**1. Where does the scope predicate live?** In the repository, at the point the `WHERE`
clause is written — `TicketRepository._scope()`, via `row_scope_predicate`. It cannot
live at the route: `TICKET_VIEW` is one capability held by four roles, and what differs
between them is how many rows it reaches. It also cannot be a post-filter in the
service: filtering a page after the database has already chosen it gives short pages
that look like the end of the data.

**2. What does an unresolvable scope mean?** `RowScope.OWN` resolves through
`TenantContext.customer_id`, which is `None` for a portal account with no linked
`Customer` row. The tempting predicate is `Ticket.customer_id == context.customer_id`.
In SQL that is `customer_id = NULL`, which is *unknown* rather than false — so it
matches nothing, which is the correct answer, by accident. That accident survives until
someone composes the predicate differently (an `or_`, a `NOT`, a wrapper that turns it
into an existence check) and it silently starts matching everything.

So `row_scope_predicate` writes the case out explicitly:

```python
if context.customer_id is None:
    return false()
```

A scope that cannot be resolved is *denied*, not *ignored*. This is asserted, not
assumed: `tests/security/test_row_scopes.py` builds the unlinked context directly —
the API refuses to create one, which is why the test has to go below the API — and
asserts an empty page while the tickets it *could* have claimed sit in the same
organization. Verified by temporarily changing that `false()` to `true()`: the test
fails, reporting all four tickets visible to an account that owns none of them. A
`true()` there is the whole vulnerability in one character.

**3. Where is message visibility decided?** `MESSAGE_SCOPE_BY_ROLE` exists and is
`dict(TICKET_SCOPE_BY_ROLE)` by construction. Rather than implementing `OWN` and
`ASSIGNED` a second time against a join, every message route **first resolves the
ticket** through `TicketRepository`, which already applies the caller's scope and
returns `None` for a ticket out of reach. Only then are messages read, filtered by
`ticket_id`.

A message is reachable exactly when its ticket is, and that is the point: it is one
implementation and one 404. The alternative is two copies of the scope on two different
queries, which is precisely the drift the central mapping exists to prevent. The map
stays as the declaration of intent; this ADR records that it is applied at the ticket.

**Related, in the same phase: internal notes are hidden from two views, not one.**
`GET /tickets/{id}/messages` withholds internal notes from a caller without
`MESSAGE_READ_INTERNAL`, and `GET /tickets/{id}/events` withholds `INTERNAL_NOTE_ADDED`
events under the same capability. Both matter, and the second is easy to miss: a
customer who can see *that* a note was written at 14:02, and not what it said, has
learned something the thread filter exists to prevent — through the side door. Two views
of one ticket contradicting each other is a bug, not a limitation.

**Cost.** Row scope is applied by hand in each repository that needs it. There is no
convention that makes forgetting it a type error, and forgetting fails open. The
mitigation is that the organization filter *is* structural
(`TenantScopedRepository._select` applies it and cannot be bypassed), so the worst a
forgetful subclass can do is over-expose *within* one tenant rather than across tenants
— and `tests/security/test_row_scopes.py` asserts the per-role answer for all four roles
and an unlinked account, so a regression is caught rather than reviewed.

---

## ADR-016 — Ticket numbers are allocated under a per-tenant advisory lock

**Status:** accepted · Phase I–K

**Context.** `tickets.number` is a per-organization sequence starting at 1 — the number
a customer quotes on the phone — and there is no PostgreSQL sequence behind it, because
a sequence cannot be per-tenant without one sequence per tenant. `app/models/ticket.py`
anticipated the consequence: the unique index on `(organization_id, number)` makes a
collision "a retryable integrity error".

Two concurrent transactions both reading `MAX(number) + 1` do collide. The obvious
answer is the one the model's comment suggests: catch the integrity error and retry.

**Decision.** Take `pg_advisory_xact_lock` on a key derived from the organization id,
then read `MAX(number) + 1` in the same transaction.

```python
await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(org)})
return int(await session.scalar(select(func.coalesce(func.max(Ticket.number), 0) + 1)) or 1)
```

**Why not the retry loop.** A failed statement in PostgreSQL aborts the *transaction*,
not just the statement. So a retry is not `except IntegrityError: try again` — it needs
a `SAVEPOINT` around the insert, a bounded attempt count, an answer for what happens
when the attempts run out, and a test that forces the exhaustion path. That is a lot of
machinery to make a collision *survivable* when a one-line lock makes it *impossible*.

The lock is transaction-scoped, so it is released on commit or rollback with no cleanup
path to forget. It is keyed per organization, so two tenants never contend. And the key
is derived, not allocated, because there is nothing to allocate: a collision between two
organizations would merely serialize two creations that did not need serializing, which
cannot produce a wrong answer.

**Cost.** Ticket creation within a single tenant serializes. For a support desk that is
one short transaction on a low-frequency write, and the alternative is a retry loop
that has to be tested. The unique index stays as the invariant backstop; the lock is the
mechanism.

**How it is verified.** Not by reasoning — a single-threaded HTTP test cannot prove
this. `test_numbers_are_distinct_under_concurrent_creation` raises eight tickets from
eight threads against one organization and asserts the numbers are exactly 1–8.
Confirmed meaningful by temporarily replacing the lock call with `pass`: all eight
threads then fail with `UniqueViolation` on `uq_tickets_org_number`, which proves both
that the lock is load-bearing and that the threads are genuinely concurrent rather than
serialized by the test client.

---

## ADR-017 — Status is an action, not a field, and each action is its own route

**Status:** accepted · Phase I–K

**Context.** The spec is explicit that "status is never mutated by a blind field update;
transitions go through an action that validates the edge". The matrix in
`docs/requirements.md` §3 makes the same point structurally: `change status`, `close`,
and `reopen` are three separate rows, and the fourth column qualifies each with a
different scope.

The obvious design is one route with a target status in the body, branching on which
capability the target requires: `PATCH /tickets/{id}/status {"status": "closed"}`.

**Decision.** Three routes, one capability each, declared statically:

| Route | Capability | Edge accepted |
|---|---|---|
| `POST /tickets/{id}/status` | `TICKET_CHANGE_STATUS` | any single edge in `TICKET_TRANSITIONS` |
| `POST /tickets/{id}/close` | `TICKET_CLOSE` | `RESOLVED → CLOSED` only |
| `POST /tickets/{id}/reopen` | `TICKET_REOPEN` | `CLOSED → OPEN` only |

**Why.** A route that chose its required capability at request time would declare *no*
capability, and `tests/security/test_route_protection.py` fails any authenticated route
that declares none — correctly, because "this route requires permission X" has to be
answerable by reading the route. Beyond the test: a static guard is visible in the
OpenAPI schema, in code review, and in a stack trace. A runtime branch is none of those.

It also keeps the route table 1:1 with the documented matrix, so the matrix can be
transcribed independently and asserted — the same property ADR-013 relies on for
`ROLE_PERMISSIONS`.

**`/close` is narrower than `/status` on purpose.** The `close` row's scope in the
matrix is "confirm resolution": an agent resolving work and a customer agreeing that it
is resolved are different acts, and only the second is `close`. So `/close` accepts
`RESOLVED → CLOSED` and nothing else, and a caller attempting it on an `IN_PROGRESS`
ticket gets `409 INVALID_TICKET_TRANSITION` with a hint naming the legal target.

**Every transition writes a `TicketEvent` in the same transaction**, and resolving or
closing sets `resolved_at`/`closed_at` in the same statement — a `CheckConstraint`
enforces it, so forgetting is an integrity error rather than a ticket whose status and
timestamps disagree.

**Reopening clears the assignment and both timestamps.** `OPEN` and `ASSIGNED` are the
lifecycle's two ways of saying "nobody owns this" and "somebody does"; a ticket in
`OPEN` with an agent set is a state the rest of the model has no reading for. It would
also break assignment outright — assigning it back to that same agent is a no-op, so the
ticket could never reach `ASSIGNED` again and would sit in `OPEN` forever. That was a
real bug, found while writing the tests, and
`test_a_reopened_ticket_can_be_assigned_to_the_same_agent_again` is its regression test.
`resolved_at` and `closed_at` are cleared for the same kind of reason: they describe the
ticket's *current* state, and leaving them set would make every SLA and duration query
wrong. Nothing is lost — the history is in `ticket_events`.

**Cost.** Three routes where one would do, and a client has to know which to call. That
is the price of the guard being readable at the route, and it is the same price ADR-013
already paid for capabilities.

---

## ADR-018 — Uploads are proxied through the API, and the storage key is server-generated

**Status:** accepted · Phase L

**Context.** Spec §33 describes the flow as
`Frontend -> FastAPI -> validated upload -> object storage -> database metadata` and ends
with "prevent path traversal". The conventional design for file uploads against S3 — a
presigned `PUT`, then a second request to record the metadata — collapses that flow to
`Frontend -> storage`, with FastAPI involved only in issuing a URL.

**Decision.** Every byte goes through the API. Uploads are `multipart/form-data` to
`POST /tickets/{id}/attachments`, downloads are a `StreamingResponse` from
`GET /attachments/{id}`, and the client never receives a URL or a storage key.

The key is `f"{organization_id}/{ticket_id}/{uuid4().hex}"`, composed entirely from
server-side values. The client's filename is stored in a column of its own for display,
with its directory stripped, and reaches neither the key nor the bucket.

**Why proxying.** A presigned URL is a capability handed to the client *and then used
without the API in the path*. Authorization cannot be enforced on a request that does not
come through the application, so tenant scoping — which holds for every row in the
database — would stop applying at the one point where the data actually leaves. The
metadata row could still be written afterwards, and the object could still be fetched by
anyone the URL was leaked to, until it expires.

There is no way to describe that as anything other than a hole in the isolation story, so
§33's first three arrows are read as the intended architecture rather than as a
description of one deployment. `test_an_organization_cannot_download_another_s_attachment`
is the assertion that the choice is load-bearing: it is a cross-tenant read of a *file*,
which a presigned URL could not have refused.

**Why a generated key.** §33's "prevent path traversal" is usually implemented as a
sanitizer over the client's filename, and a sanitizer is a list of the attacks someone
thought of. Composing the key from `organization_id`, `ticket_id`, and a fresh `uuid4`
makes traversal unrepresentable: there is no client string in the key, so there is
nothing to traverse with. The test asserts the *shape* of the key — three slash-separated
components, the last exactly 32 hex characters — rather than scanning it for `..`, because
the shape is the reason, and a scan would pass for a key that happened to be safe.

The filename is still sanitized for display, since it is echoed back in
`Content-Disposition` and stored on the row. That is a correctness concern, not a security
boundary: `../../shot.png` is *accepted* — every signal the validator can check is
consistent — and simply displays as `shot.png`.

**Consequences.**

- The API is on the hot path for every byte, so a large upload occupies a worker. It runs
  in a thread (`anyio.to_thread.run_sync`) because `boto3` is synchronous and would
  otherwise block the event loop for the duration — the same class of mistake ADR-011
  exists to prevent. Size is capped at `MAX_ATTACHMENT_BYTES` (25 MiB) and counted as the
  bytes stream in, not read from `Content-Length`.
- `ensure_bucket()` runs in the lifespan **before** the `yield`, and a failure is logged
  as a warning rather than raised: storage being down should not stop the API from serving
  tickets. An upload attempted while storage is unreachable returns
  `503 STORAGE_UNAVAILABLE`, which a client can retry, rather than an opaque 500.
- Download responses carry the *detected* content type, `X-Content-Type-Options: nosniff`,
  and `Content-Disposition: attachment`. Without the last two a browser may render a
  stored file in the API's own origin, which turns an accepted upload into stored XSS.
- Deleting a ticket cascades to the attachment rows and removes nothing from the bucket.
  Orphaned objects need a lifecycle policy or a sweeper, and neither belongs in a request.
  It is a documented limitation rather than a silent one.

---

## ADR-019 — An attachment is reachable exactly when its message is

**Status:** accepted · Phase L, correcting a prediction in ADR-013

**Context.** ADR-013 introduced `ATTACHMENT_SCOPE_BY_ROLE` alongside `TICKET_SCOPE_BY_ROLE`
and `MESSAGE_SCOPE_BY_ROLE`, and predicted that the attachment map would be the first to
diverge: "an attachment on an internal note is not visible to the customer who owns the
ticket". Phase L is where that prediction came due.

**Decision.** The prediction was right about the rule and wrong about the shape. The role
map does **not** diverge — reading the matrix in `docs/requirements.md` §3, admin is
`all`, manager is `all`, agent is `assigned`, customer is `own`, which is character for
character what the ticket map says. `ATTACHMENT_SCOPE_BY_ROLE` stays
`dict(TICKET_SCOPE_BY_ROLE)`, and `test_the_three_scope_maps_agree_today` stays green.

The divergence the prediction described is a **row** rule, not a role rule, and it is
resolved the way message visibility already is: an attachment has no `customer_id` and no
`assigned_agent_id`, so `row_scope_predicate` has no columns to take for it. It is reached
through its ticket.

So the rule is applied in two layers, both mirroring `MessageRepository.list_for_ticket`:

1. **The route resolves the ticket first**, through the same
   `ticket_service.require_visible_ticket` the ticket routes use. An agent who cannot see
   the ticket cannot see its files. `AttachmentRepository` is deliberately *not* row-scoped
   for the same reason `MessageRepository` is not.
2. **On top of that, one row rule:** an attachment whose `message_id` names an internal
   note requires `MESSAGE_READ_INTERNAL`. Without it the attachment is a 404,
   indistinguishable from one that does not exist. Applied by default in both
   `list_for_ticket` and `get_visible`, so a caller that forgets the flag gets the
   restrictive answer rather than the permissive one.

This is ADR-015's shape applied one level deeper: the scope is resolved where the query is
built, and a scope that cannot be resolved from the row's own columns is resolved by
walking to the row that has them.

**The correction made while testing it.** The download route has three ways to refuse —
no such attachment, an attachment on an unreachable ticket, and an attachment on an
internal note — and the docstring claimed all three were indistinguishable. Two were not:
`require_visible_ticket` raises `TICKET_NOT_FOUND`, so an attachment id on someone else's
ticket reported a *different* error code than an id that never existed. A caller holding
an id could therefore tell "this file exists, on a ticket that is not mine" from "no such
file", which is precisely the oracle ADR-009 exists to remove — and exactly the leak a
route that reports the wrong resource name produces. `get_attachment` now catches that and
re-raises as `ATTACHMENT_NOT_FOUND`: the route is `/attachments/{id}`, so an attachment is
what the caller asked for and an attachment is what is missing.

`tests/security/test_row_scopes.py` found it, by asserting the two 404 bodies are
byte-identical rather than merely both 404.

**Cost.** Two layers to reason about, and a caller must resolve a ticket before it can
reach a file. That is the price of an attachment having no scope of its own, and it is
what makes the customer-facing rule hold without splitting the role map.

**Revisit when a message-delete route is added.** `docs/data-model.md` records that
`attachments.message_id` is `SET NULL` on message deletion, so an attachment outlives the
message it arrived with — and an attachment on an internal note would lose its internal
marking and become ticket-level. There is no message-delete route today, so the path is
unreachable; a comment in the code names it, and the README lists it as a limitation.

---

## ADR-020 — Audit rows are written in the actor's own transaction

**Status:** accepted · Phase M

**Context.** §34 lists the actions worth recording. `audit_logs` existed from Phase D
with four indexes, `AuditAction` already enumerated §34's list, and `Permission.AUDIT_VIEW`
was already held by admin alone — but nothing wrote a row. The table was empty.

The design question is not what to record but *when* the record commits. The tempting
shape is a small helper called after the action, opening its own session so a failure to
audit cannot fail the action.

**Decision.** `audit_service` follows `ticket_service.record_event`, the closest analogue
and the pattern the project already trusts: **it never commits.** `record()` takes a
session and adds a row; the caller's transaction commits both or neither. Two entry points
exist because registration has an actor and no `TenantContext` yet:

| Function | Identity from |
|---|---|
| `record(session, *, organization_id, actor_user_id, actor_email, action, ...)` | explicit arguments |
| `record_for(session, context, action, *, target_type, target_id, ...)` | the authenticated context |

Every wired call site uses `record_for`, so no call site can stamp the wrong actor —
the identity comes from the request, not from the arguments. `record` exists only for
registration, where there is no context to take.

**Why one transaction.** An audit trail that can disagree with the data it audits answers
no compliance question. A helper with its own session creates exactly that: the ticket is
assigned and the audit write fails, or worse, the audit write succeeds and the assignment
rolls back, leaving a trail describing something that never happened. Sharing the
transaction makes the two states identical by construction instead of by a retry policy —
and it is the same reasoning that puts `ticket_events` in the actor's transaction.

**Request provenance.** `client_ip`, `user_agent`, and `request_id` are threaded in as an
explicit `Origin` dependency rather than set on a `ContextVar` by middleware. A
`ContextVar` would be invisible at the call site — a reader cannot tell from `record_for`
that it captures anything — and untestable without driving a real request. An explicit
parameter is both.

`TenantContext` gained one field for this: `email`, denormalized into the row. It exists
because `AuditLog.actor_email` is there precisely so a row survives its actor's deletion
(`actor_user_id` is `SET NULL`), and `TenantContext` had no email to put in it. The field
is commented as investigation-only, like `ip_address` beside it, and is never read for a
decision.

**`before` and `after` land in `extra_data`**, satisfying §34's "before/after values where
appropriate" and mirroring the `from_value`/`to_value` pair on `ticket_events`, so the two
views of one change agree rather than merely both existing.

**What is deliberately not audited.** Customer and message writes. `AuditAction` is a
PostgreSQL enum with no member for either, so adding one is a migration; and both are
already fully attributable — a customer's creation is implied by the ticket that names
them and by `ticket_events`, and every message writes an event carrying its authenticated
actor. Adding enum members is deferred to a phase that needs them rather than done
speculatively.

**Registration is audited**, which has a consequence worth stating: a fresh organization's
trail is never empty. It holds exactly one `user_created` row for the founding
administrator, with `metadata={"source": "registration"}`. The tempting test assertion
("a new organization's audit list is empty") is therefore wrong, and the right one is that
a second organization cannot see those rows. The test says so in as many words.

**Cost.** Every audited route gained a parameter, and a service that never commits is a
convention a future caller can break by committing early. The repository exposing only
`add` and the list methods — asserted by a test that the module defines no `update` or
`delete`, and by `AuditLog` having no `updated_at` — is what keeps "append-only" checkable
rather than aspirational.

---

## ADR-021 — Search narrows and can never widen; index reachability is measured

**Status:** accepted · Phase N

**Context.** §14 asks for search across ticket number, subject, description, customer
name, customer email, and message content, plus filtering, sorting, and date ranges.
`ix_tickets_fts` had existed since Phase D and no query had ever used it.

**Decision.** One predicate builder, `ticket_search_predicate(term, *, include_internal)`
in `app/repositories/search.py`, returning a single disjunction over four arms:

```sql
(
     to_tsvector('english'::regconfig, (subject::text || ' '::text) || description)
       @@ websearch_to_tsquery('english'::regconfig, :q)
  OR CAST(number AS TEXT) = :q
  OR EXISTS (SELECT 1 FROM messages  WHERE ... AND to_tsvector('english'::regconfig, body) @@ ...)
  OR EXISTS (SELECT 1 FROM customers WHERE ... AND (name ILIKE :pattern ESCAPE '\' OR email ILIKE ...))
)
```

**Search narrows; it never widens.** Every caller ANDs this with the caller's scope
predicate, and no code path in the module returns a predicate usable *instead of* a scope.
That is worth stating as a design property rather than a happy accident, because the
classic form of this bug is a query parameter that grants access — and `q` is a query
parameter. `test_search_cannot_widen_an_agents_row_scope` states it directly: the term
appears only in a colleague's ticket, and the agent gets an empty list *and* the same 404
the ticket route gives.

**`include_internal` is the phase's one real hazard.** A customer holds `MESSAGE_LIST` and
reaches their own ticket, so without this the message arm would let them find a ticket by
typing a phrase that appears only in a note they cannot read — search becoming the way
around a filter every other route applies. The caller passes
`context.has(Permission.MESSAGE_READ_INTERNAL)` and the arm is **dropped entirely** rather
than filtered afterwards: an internal-note row that is fetched and then discarded is a row
that was read, and the version of this bug that matters is the one where the filter is
applied one query too late. Asserted from both sides — the customer must not find it, and
the agent must, so the filter is narrow rather than merely present.

**A term is data, not a pattern.** `escape_like` handles the LIKE metacharacters and moved
here from `customer_repository.py` unchanged, because the ticket search needed the same
function and that is the moment a helper stops belonging to one caller. Trivially, `100%`
searches for the customer named `100% Cotton` instead of matching every row.
`websearch_to_tsquery` rather than `plainto_tsquery`: it cannot raise on malformed input
and it understands the quoted phrases a support agent types.

**The FTS expression is imported, not restated.** `TICKET_FTS_EXPRESSION` and
`MESSAGE_FTS_EXPRESSION` are module-level constants declared by the models that own the
indexes, and both the `Index(...)` and the query read the same object. PostgreSQL matches
an expression index to a query by the expression *as text*, so the prettier
`to_tsvector('english', subject || ' ' || description)` would silently lose the index —
no error, no wrong rows, just a sequential scan. Sharing the constant makes the divergence
unrepresentable rather than merely discouraged, and
`test_the_search_predicate_contains_each_indexed_expression_verbatim` compares the two
strings exactly, with no normalization, because exact is what the planner does.

**The measurement, and what it showed.** With 5,000 tickets, 20,000 messages, and 20,000
customers seeded in one organization, `EXPLAIN (ANALYZE, BUFFERS)` gave:

| Query | Plan | Time |
|---|---|---|
| ticket FTS arm alone | Bitmap Index Scan on `ix_tickets_fts` | 1.5 ms |
| message FTS arm alone | Bitmap Index Scan on `ix_messages_fts` | 3.8 ms |
| customer name `ILIKE` alone | Seq Scan — index declined | 19.4 ms |
| full predicate, real term | Index Scan Backward on `ix_tickets_org_created_at` + filter | 42.8 ms |
| full predicate, term matching nothing | same, 5,000 rows removed by filter | 103.2 ms |
| `sort=priority desc` | Seq Scan + top-N heapsort | 3.7 ms |
| `sort=name desc` on customers | Seq Scan + top-N heapsort | 18.6 ms |

- **The four-arm OR does not use either full-text index.** The planner estimates the
  disjunction at roughly 75% of the tenant's rows — there is no selectivity function for
  `@@` against a generic `tsquery`, and the two `EXISTS` arms are hashed subplans of
  unknown selectivity — and at that estimate, walking the tenant's `created_at` index in
  sort order and filtering is genuinely the better plan. It is acceptable at these sizes
  and it is **linear in the tenant's ticket count**, which the last row shows: a term
  matching nothing still costs a pass over every ticket the organization has. The
  individual arms are index-driven, so the reachable fix is to **union** the arms and
  deduplicate instead of OR-ing them. That changes the query's shape and its ordering
  semantics, so it is deferred and documented rather than smuggled in with the feature.
- **`ix_customers_name_trgm` works and the planner declines it.** Forced with
  `enable_seqscan = off` it runs in 3.1 ms against the seq scan's 18.6 ms, but the
  planner's costs are 928 versus 608 the other way. GIN cost estimation is pessimistic
  here; the index is chosen as the table grows and nothing needs doing.
- **`ix_customers_org_name` was not added**, which is the decision the plan said this
  measurement would make. `sort=name` is 18.6 ms for 20,000 customers and the Sort node is
  50 of the plan's 1,301 cost — the scan dominates, not the sort, and that was the stated
  condition for adding it.

**Why `priority` sorts by declaration order.** The PostgreSQL enum is ordered
`LOW < MEDIUM < HIGH < URGENT`, so `order=desc` leads with `URGENT`. Sorting the label
alphabetically would put `HIGH` first. Correct, and not obvious enough to leave unstated —
so it is in the route docstring and pinned by a test.

**Why `id` is always appended to the sort.** Offset pagination over equal sort keys
repeats and skips rows, because PostgreSQL is free to return equal rows in a different
order per query. It only shows up under load, which is exactly when it is expensive to
find. `test_pages_over_equal_sort_keys_do_not_repeat_or_skip_a_row` pages over six
deliberately-equal priorities and asserts the two pages are disjoint and their union is
everything.

**Cost.** The four-arm OR is one index-unfriendly query instead of four index-friendly
ones, and the honest measurement above is what that costs. Sorting added four enum values
and a parameter to each list route, and the search term is capped at 200 characters
because it reaches `websearch_to_tsquery` and two `ILIKE` patterns.

---

## ADR-022 — Redis's responsibilities arrive with their consumers

**Status:** accepted · Phase O

**Context.** §15 gives Redis four jobs: rate limiting, caching, Pub/Sub, and short-lived
task/state coordination. Three phases after Redis was first wired in, the tally was one
of four — and "one of four" reads as three omissions unless the other three are placed
deliberately. §15 also closes with "Do not cache everything blindly", and §223 says "Do
not use technologies merely for resume keywords", so building an unused cache and an
unsubscribed Pub/Sub channel to fill out the list would violate the spec in the act of
satisfying it.

**Decision.** Redis carries rate limiting today, and each remaining responsibility is
assigned to the phase that gives it a consumer. The client is consolidated into one
object with one lifecycle at the same time, because three places were building their own.

**Three clients, one server, no shared view of it.** `rate_limit.py` held a private
`_shared` global; `app/api/health.py` called `aioredis.from_url(...)` **on every readiness
probe** and closed it in a `finally`. That second one was a defect rather than a style
problem, and it is why this consolidation needed no future phase to justify itself. It was
measured rather than asserted — `redis-cli info stats`, twenty `GET /health/ready`
requests, the counter read before and after:

| Probe implementation | `total_connections_received` delta over 20 probes |
|---|---|
| A client built and closed per probe | **23** |
| The process's shared client | **2** |

`get_client()` in `app/core/redis.py` is now the only place a client is built. It is lazy,
so importing the module never requires Redis to be up, and `Redis.from_url` opens no
socket — which is also why the module holds **no logger**: there is no failure here to
report, and the only thing that can go wrong is a connection error, which surfaces at the
call site with the request that caused it. That keeps `tests/security/test_log_hygiene.py`
honest, since its module list is hand-maintained and would otherwise need an entry for a
module that never logs.

The probe now **borrows** the client and does not close it. Its `aclose()` was correct
while it owned what it closed; on a shared pool it would tear down the connections the
rate limiter is using, and the symptom would be a limiter that fails open intermittently
for reasons having nothing to do with limits.

**Upload is limited per user. Login and registration are limited per address. This is
deliberate and they are opposites.** At login there is no identity yet — that is the whole
point of the endpoint — and the attack is credential stuffing spread across many accounts,
so the address is the only available key. Upload is authenticated: identity exists, and
§45's concern is one account filling the object store. Keying upload on the address would
let one person's backlog throttle an entire office behind a NAT, which is cost #2 that
ADR-014 already records against the login limiter. That trade is forced there and avoidable
here, so it is not repeated. `test_the_upload_limit_counts_the_user_not_the_address` states
it: with a limit of one, the same account's second upload is refused while a colleague's
first is served, over the same ticket, from the same client address.

**The refusal happens before the file is examined.** A route dependency is resolved before
the path operation's own parameters, so the guard runs before `file: UploadFile` is read.
Confirmed over real HTTP rather than reasoned about: a request that is both over the limit
and carries a disallowed file type returns **429**, while the identical request against an
account under its limit returns the validation error. The status code is what identifies
which check ran first. The honest boundary is that uvicorn has already taken the bytes off
the socket — this stops the work, not the upload of the bytes — and the `Retry-After`
header is 3600 because the window is an hour.

**Where the other three responsibilities land, and why.**

- **Cache → Phase S.** §15's own example is dashboard analytics, and dashboards are Phase
  S. The valuable half of this entry is the *rejection*: the obvious optimization is
  caching the `users JOIN organizations` row that `deps.get_current_user` loads on every
  authenticated request. **Refused.** ADR-013 checks `is_active` and the organization's
  `status` on every request specifically so that deactivation and suspension take effect
  on the *next request, not one TTL later*. That join is not measurably slow, and caching
  it would trade a stated security property for an unmeasured performance one.
- **Pub/Sub → Phase R**, where a WebSocket connection manager subscribes. On this backend
  a channel with no subscriber is §223's warning wearing infrastructure.
- **Short-lived task and state coordination → Phase P**, with Celery. `config.py` does not
  read the `CELERY_*` variables yet, for the same reason it did not read `S3_*` before
  Phase L: settings arrive with the code that uses them, verified rather than predicted.
- **§45's remaining two limiters** — AI endpoints and knowledge-base processing — go with
  the phases that build those endpoints (T–W and X). No setting exists for an endpoint
  that does not.

**The limiter's own behaviour is unchanged and stays where it was decided.** `RateLimiter`
is untouched, including `_hit`'s `SET ... NX EX` before `INCR`, which is what guarantees a
counter never exists without a TTL and therefore cannot become permanent. Failing open when
Redis is unreachable remains ADR-014's decision, with its costs, and is not restated here.

**Database separation.** Compose already sets limits and cache on db 0 and the Celery
broker and result backend on db 2. Worth stating as a decision rather than leaving as a
URL in a compose file: a `FLUSHDB`, or a `KEYS` sweep run to clear one purpose, must not be
able to take out another. Tests use db 1.

**Cost.** One more module and one more indirection between the guards and their client — in
exchange for one pool, one shutdown path, and one answer to "is Redis up?". Upload is now
capped at 60/hour per user by default, which a legitimate user will never reach and a
script filling the bucket will; the ceiling is a setting because the right number depends
on how much a given desk attaches. Nothing is cached, so every authenticated request still
does its join — which is the cost ADR-013 already accepted, and this ADR declines to pay
back.

---

## ADR-023 — The notification is written in the request; only the email is queued

**Status:** accepted · Phase P

**Context.** §26 asks for in-app notifications on seven events, records persisted, and a
read/unread state. §16 lists eight background task categories and §51 says Phase P should
"start with notification tasks". The obvious implementation is the one Celery's own
documentation leads you to: the request publishes a task, the task writes the notification
row and sends the email. It is also wrong here, and the reason is the ordering.

**Decision.** `notification_service.notify_for_event` writes the row inside the *request's*
transaction and never commits — the shape ADR-020 already established for audit rows. What
is queued is only the part that leaves the process: SMTP.

**Why the task must not write the row.** A broker is not durable the way a database is. A
Redis restart without persistence loses queued messages, and if the row only ever existed
because a task created it, those notifications were never written and nothing anywhere
records the loss — no row, no badge, no error, just a user who was never told. The other
direction is worse in a subtler way: a task that both writes the row and sends the mail, run
twice, produces two rows and two emails, and the retry that causes it is something
`acks_late` makes routine. Writing in the request's transaction makes the notification and
the change it describes **one commit**, so a failed assignment cannot leave a notification
about an assignment that did not happen.

`enqueue_delivery` is therefore called by the caller *after* the commit, and the task is
handed **an id and nothing else**. Copying the title and body into the broker would create a
second copy of a stored fact, and the two would disagree the first time either changed. It
is also why the ordering is load-bearing rather than stylistic: the task's first act is to
read that row, and a task enqueued before the commit races a transaction it cannot see.

**The task's own measurement, since the claim above is empirical.** With the worker
**stopped**, assigning a ticket wrote the row and left `emailed_at` NULL, with one message
sitting in Redis db 2. Starting the worker delivered it 5ms later and stamped the row. The
record survived a worker that was not running, and the queue held the delivery until one
was.

**At-least-once, and what `emailed_at` narrows it to.** `task_acks_late=True` is what stops
a worker killed mid-send from silently losing an email, and its price is that a worker dying
*after* sending but *before* acknowledging is handed the same task again. Without a guard
that is a duplicate email. `notifications.emailed_at` — a nullable timestamp, the idiom the
model already uses for `read_at` — makes the task return early on a row that already carries
one, which narrows the window from "any redelivery" to "a crash between the send and the
mark". It is **narrowed, not closed**, and closing it would need the send and the mark in one
atomic step, which SMTP is not part of. The honest description is "at-least-once, with a
window one statement wide". Verified rather than asserted: re-queueing a stamped
notification logged `reason=already-sent` and sent nothing.

**Retry only what retrying can fix.** `autoretry_for=(TransientEmailError,)` — a connection
refused or a socket timeout is worth another attempt; a refused recipient or a bad
credential is not, and five attempts with backoff would be five minutes spent re-sending a
message the server has already rejected. `PermanentEmailError` is outside the tuple, so it
fails the task on the first attempt and the traceback lands in the worker's log.

**The backoff is in minutes, and the first version was not.** It was written as
`retry_backoff=True`, which is one second. The reasoning in the comment was about a mail
server that is *down*, and a server down for a restart is down for longer than a second —
all six attempts landed inside about fifty seconds and gave up while the server was still
booting, which is exactly the failure the policy exists to survive. It was caught by running
it rather than by reading it: pointed at a closed port, the observed countdowns were 1s, 1s,
3s, 8s, 10s. `retry_backoff=60` gives nominal countdowns of 1, 2, 4, 8 and 10 minutes (the
last capped by `retry_backoff_max`), each drawn uniformly below its nominal by
`retry_jitter`. A failed delivery leaves the row looking exactly like one never queued —
`emailed_at` NULL, which is the query a backlog sweep would use.

**`smtplib`, and no new dependency.** There is no mail library in `pyproject.toml` and none
is needed: Celery tasks are synchronous by design and `smtplib` is. `app/core/mail.py` is
the only module that imports it, the same boundary role `app/core/storage.py` plays for S3,
and it is where the library's exceptions become `TransientEmailError` /
`PermanentEmailError`. `EmailMessage` composes the message.

**No pickle, ever.** `task_serializer` and `result_serializer` are `json` and
`accept_content` is `["json"]`. This is not the default-was-fine case. Pickle is remote code
execution by design — a worker unpickling a message from the broker runs whatever the
payload's `__reduce__` says — and the broker here is Redis, which anything on the network
can sometimes reach. §4 does not allow that one configuration typo away, so the allowlist
makes it impossible rather than discouraged.

**The worker runs its own loop, and therefore its own engine.** A task body reaches the
database through `app/core/event_loop.run` (ADR-011), which builds a new loop per call. That
is fine for the connection and fatal for a *pooled* one: a psycopg connection is bound to
the loop that opened it, so the second task would be handed a connection belonging to a loop
that has since closed. `email_tasks.py` therefore builds its own engine with `NullPool`.
`app/core/database.py` warns against a second engine, and the warning is about multiplying
connections; this does not, because the worker never imports that module — the API's pooled
engine does not merely go unused there, it does not exist.

**One queue, and `task_always_eager` deliberately absent.** §51 asks for task routing, and a
route table with one entry is the honest amount of it for one task type: it makes the
destination explicit instead of relying on the default queue's name. The `ai` (T–W),
`reports` (S) and `knowledge` (X) queues are not pre-declared, because a queue nothing
publishes to is a worker process waiting for work that does not exist. Eager mode is left
off here *and* in the test suite, which is the opposite of the usual instinct: eager makes
`.delay()` run the task inline, inside the request that produced the notification, and the
task body would then start a second event loop inside the one already serving that request.
`asyncio` refuses, so turning it on would fail every request that produced a notification
rather than merely testing differently. The suite records the enqueue instead and exercises
the task body from a context that owns its loop.

**No Celery beat.** §48 says "celery beat where needed", and Phase P needs it nowhere: every
task here is event-driven, because a notification exists exactly when a request created one.
The first thing that needs a schedule is SLA monitoring in Phase Q, and the beat service
arrives with it. Same restraint as ADR-022's Pub/Sub.

**A "ticket resolved" notification goes to the customer's portal login, and a customer
without one gets nothing.** `notifications.user_id` is NOT NULL, so the recipient is always
a *user*. §26's recipient for a resolution is the customer, and the only user who is that
customer is the portal account linked by `users.customer_id`. A customer record with no
login has nobody to notify, so **no row is written** — not a row addressed to the agent who
resolved it, which would be telling someone about their own action. This is a real
limitation, stated in the README rather than papered over by inventing a second recipient
model §26 does not describe.

**And it goes to *every* portal login, which was a bug first.** Resolution originally used
`scalar_one_or_none` on `users.customer_id` and relied on the link being unique. Nothing
enforces that, and the second login turned a resolution into `MultipleResultsFound` — a 500,
on the happy path, for a customer an admin had given two accounts. The fix is not a
tie-break: the list-returning shape of `notify_for_event` was already built for
multi-recipient events, and no product rule forbids a second login, so all of them are
notified. `list_by_customer_id` returns a list and the repository method's name says so. It
is worth recording that the first version's docstring argued the raise was "a better failure
than picking one arbitrarily" — which was rationalizing a 500, and is now corrected in the
code.

**Manager mentions are deferred, deliberately.** The phrase "manager mention" occurs exactly
once in the 2730-line specification — in §26's bullet list — with no syntax, no resolution
rule, and no UI anywhere else. `NotificationType.MENTION` stays in the enum, unreachable,
and this paragraph is the record of it. The events that produce a notification today are the
four with producers: assignment (and reassignment, distinguished by `TicketEvent.from_value`
being non-null), a customer reply, and a resolution. `CREATED`, `UNASSIGNED`,
`PRIORITY_CHANGED`, `INTERNAL_NOTE_ADDED`, `ATTACHMENT_ADDED` and `REOPENED` produce none,
because §26 does not list them — stated explicitly rather than left to omission, and pinned
by a test.

**Authorship is read from `message.sender_type`, not from the caller's role.** `MESSAGE_ADDED`
records that the thread grew, not who grew it, and §26's distinction — "new customer reply" —
is exactly the sender type. Reading it off the role instead would misfile an `AI_DRAFT`,
which has no role and must never be mistaken for the customer writing in (§41).

**Cost.** Three explicit calls now sit at each mutating call site (`record_event`,
`audit_service.record_for`, `notification_service`), which is the established shape here and
a thing to forget. That is what `tests/security/test_route_protection.py`-style sweeps are
for, and `tests/unit/test_notification_policy.py` pins every `TicketEventType` to a
recipient or to explicitly nothing, so a new event type that nobody decided about fails
there rather than going quietly unnotified. The delivery is at-least-once, and the window is
one statement wide. A notification written while the worker is down is delivered when one
starts, but a notification whose five retries are exhausted is **not** retried again — the
row stays unstamped for a sweep that Phase Q or later will have to write.



## ADR-024 — The SLA clock is derived on every read; only the alert is stored

**Status:** accepted · Phase Q

**Context.** §27 gives four priorities a first-response and a resolution target, on a sample
table whose closing paragraph asks that the numbers not be presented as industry standards.
§26 names "SLA warning" as an event worth a notification. §48 says "celery beat where
needed". Phase Q is the first phase with work whose trigger is *the passage of time* — no
request causes it and no user is waiting for it — and it is the phase five earlier places
were left waiting for, each in writing: `SILENT_EVENT_TYPES` held `SLA_BREACHED` with a
comment saying Phase Q would know whether a breach is a second alert or a correction of the
first; `DEFERRED_EVENT_TYPES` held `SLA_WARNING`; the notification-policy test said of its
own deferred case "this is the test that will need changing"; `test_celery_wiring.py`
asserted an empty beat schedule with a comment naming SLA monitoring as the first thing that
would need one; and ADR-023's own "No Celery beat" paragraph said the same. This decision is
the answer to all five, and closing them is part of the work rather than a side effect of it.

**Decision 1 — the position is computed, never stored.** Every input already exists as a
column: `tickets.created_at` starts both clocks, `first_response_at` and `resolved_at` stop
them, `tickets.status` decides which, and the organization's `SLAPolicy` for
`tickets.priority` supplies the targets. `sla_service.resolve_position` is a pure function of
those facts, and **the API and the scheduled sweep call the same one**. A stored `sla_due_at`
would be a second copy of a derivable fact, and it would go stale the moment a ticket was
reprioritised — the specific bug `notification_service` already warns about when it refuses
to parse `from_value` back into structured data. The mechanical proof that nothing was stored
is `alembic check` after this phase: **no new upgrade operations** at all, against a phase
that touched five files and added a background job.

**Decision 2 — the alert record is a `TicketEvent`, and the cost is an extra read.** What is
*not* derivable is "have we already told somebody", and that has to be written down. The
timeline is where it belongs — "SLA warning fired at 14:32" is what an agent wants to read on
the ticket — and the schema was built for it: both `TicketEventType` members already existed
with no producer, and `TicketEvent.actor_user_id` is already nullable and already commented
*"NULL when the system acted rather than a person — SLA breaches and completed AI analyses
have no actor."* The alternative was four nullable columns (`sla_response_warned_at`,
`sla_resolution_warned_at`, and two for breaches) whose only reader is one query and whose
content duplicates the timeline. The cost of the choice is real and is named here: the sweep's
"already warned?" guard is a query against `ticket_events` rather than a null check on the
ticket row. **It is not a `NOT EXISTS` per ticket** — that was the first shape and it is a
worse one, because the same rows are also what the API reports as `warned_at` and
`breached_at`, so a `NOT EXISTS` would answer the guard's question and then require a second
query to answer the display's. `sla_repository.find_alerts` fetches the page's SLA entries in
one `IN (…)` and `sla_service.index_alerts` turns them into "already warned, already
breached": one query serving both, and therefore no way for them to disagree about what has
been said. It is affordable because the candidate set is narrowed first by
`tickets.ix_tickets_sla_pending` — the partial index whose comment reads *"SLA sweep: scans
unresolved tickets only"*, written in Phase D for this query — and because
`ix_ticket_events_ticket_created` leads with `ticket_id`, so the `IN` is one index probe per
candidate. **No new index was added.**

**At most four alerts per ticket, ever.** Two timers, two states, each behind its own guard.
A ticket nobody touches for a week on URGENT produces one warning and one breach for its
response timer and one of each for its resolution timer, and is silent forever after. It is a
bounded, checkable property and it is checked by *running the sweep twice*: the second pass
returns all zeros and writes nothing.

**Decision 3 — the sweep has no `TenantContext`, and fabricating one is the failure the
context exists to prevent.** `check_organization_sla` runs with no request, no token, and no
authenticated identity. `TenantContext.user_id` is a required `uuid.UUID` and the module's
whole argument is that every field is derived from an identity — so a synthesized context
would carry a user id that is not a user and a role that decides permissions and describes
nobody, which is the class of thing ADR-009 and §4 forbid. The answer is the precedent
already in the codebase: `audit_service.record` exists beside `record_for` for exactly this
reason, and `user_repository.find_users_by_email_across_tenants` is a module-level *function*
rather than a method *"so it cannot be reached for by accident while holding a scoped
repository."* `app/repositories/sla_repository.py` follows that shape — no class, module-level
functions taking `organization_id` explicitly — and its docstring enumerates every function,
because the value of the precedent is that the set of context-free queries stays countable
and auditable. The notification rows are built with `session.add` for the reason
`audit_service.record` gives: a repository constructed from a context that does not exist
"would add a constructor requirement and nothing else".

This is the first code in the project that reads tenant data with no caller, so it is also
the first where a missing `organization_id` clause is a cross-tenant leak that no API test
can reach — the endpoints never call these functions. `tests/security/test_sla_isolation.py`
drives the sweep once per tenant for that reason, and asserts the rows the *other* tenant
never got.

**Decision 4 — a breach is a second alert, not a correction of the first.** §26 names only
the warning; the breach is this phase's addition, on the grounds that "you have 20 minutes"
and "you are 40 minutes late" were both true when they were sent and call for different
responses. Nothing is retracted when the second goes out. This needed one new
`NotificationType`, and the migration that adds it is **hand-written**: Alembic's autogenerate
does not detect enum member additions, so a phase that added the member, ran `alembic
revision --autogenerate`, and got an empty revision would discover the gap in production the
first time an SLA breach was written — in a scheduled code path nobody is watching.

**That migration's downgrade is a no-op, and the upgrade had to become idempotent because of
it.** PostgreSQL has no `ALTER TYPE … DROP VALUE`; removing an enum member means recreating
the type and rewriting every column that uses it, and the alternative — deleting the
`'sla_breached'` rows first — destroys data to undo a schema change. So there is genuinely
nothing for `downgrade()` to do. It **raised** in the first draft, on the reasoning that a
sentence is more honest than a silent no-op, and that was wrong: the effect was that
`downgrade base` — the command `tests/integration/test_migrations.py` runs to prove the schema
can be torn down and rebuilt — stopped working for every migration in the chain in order to
report a fact about one value. A downgrade that cannot run is not a more honest downgrade.
The honesty belongs in the docstring. The second half is the consequence: a downgrade that
leaves the label behind means a later `upgrade` re-runs `ADD VALUE` and finds it there, so
the statement is `ADD VALUE IF NOT EXISTS`. The suite caught exactly that, as `DuplicateObject:
enum label "sla_breached" already exists` on the round-trip — which is why the two decisions
have to be made together, and why the reasoning is recorded rather than the one-word fix.

**Decision 5 — wall-clock UTC, from `created_at`, with no pause.** The specification mentions
business hours, calendars and timezones nowhere (checked case-insensitively across all 2730
lines); §27's figures are bare durations. So both timers run continuously from creation.
`WAITING_FOR_CUSTOMER` does **not** pause the resolution clock: pausing means storing elapsed
pause time, which is a schema change and a rule the specification does not state. This is a
limitation and is listed as one in the README, not left as a silent default.

**Decision 6 — the assignee and every active manager, which §27 does not specify.** §27's
"notify agents/managers when appropriate" is a fan-out it leaves open. The manager owns the
queue, so a deadline on a ticket nobody picked up is their business rather than nobody's —
which is why an unassigned ticket alerts the managers alone and **never alerts nobody**. A
system alert with no recipient is the exact failure the feature exists to prevent. Admins are
excluded: §3 gives them every capability, so "who may act" cannot separate them from a
manager, and an alert set that included everyone who could act would be indistinguishable
from the notification centre. Deactivated managers are skipped, for the reason
`_portal_users_for` drops inactive portal logins: a row addressed to an account that cannot
sign in is one nobody reads. **Which roles those are lives in `core/permissions.py` as
`SLA_ALERT_ROLES`**, not as a `UserRole.MANAGER` comparison at the query site — the
role-comparison guard in `tests/unit/test_permissions.py` rejected the first version, and it
was right to: "who owns the queue" is a decision that belongs beside the matrix that decides
what owning it means.

**Decision 7 — two Celery tasks and a second queue.** `check_sla_deadlines` is beat's entry
point: it reads the active organization ids and dispatches one `check_organization_sla` each.
Two tasks rather than one loop over the fleet, so that one tenant's pathological backlog
cannot delay every other tenant's alerts and a failure is logged against the tenant that
caused it. It is also the fan-out shape the report and knowledge phases reuse.

The sweep runs on its own `sla` queue. The `notifications` worker holds a slot for up to the
full SMTP timeout; the sweep touches no SMTP and wants to finish in seconds, so sharing one
queue would mean a mail server outage delays every alert and a slow sweep delays every email.
**This revises ADR-023's "one queue"**, which was the honest amount for one task type and is
not for two. The price of two queues is the classic Celery deployment trap: a worker started
without `-Q sla` consumes nothing at all while looking perfectly healthy — it connects,
reports ready, and leaves every SLA task sitting in Redis forever, with no error, no metric
and no log line, because from Celery's point of view nothing is wrong. That cannot be reached
by a test that starts no worker, so it is checked where it is decided:
`tests/integration/test_celery_wiring.py` parses `docker-compose.yml` and the `Makefile` and
asserts the worker command names **every** queue in `task_routes`, so the two cannot drift.

**Decision 8 — every route that returns a ticket returns its clock.** The first draft
decorated `GET /tickets` and `GET /tickets/{id}` only, reasoning that the six mutation routes
echo the ticket back rather than reporting it. That was wrong, and the reason is a Pydantic
default: `TicketRead.sla` is `TicketSLARead | None = None`, so an undecorated response does
not *omit* the field — it serializes `"sla": null`, which is the identical payload a portal
caller receives. A client doing `setTicket(await assign(...))` would watch the countdown
disappear the moment somebody assigned the ticket, with no way to tell that from the
authorization case. One module-level helper in `app/api/tickets.py` now serves all eight
routes, which makes "a ticket response carries its clock" a property of the module rather
than of eight call sites. `tests/api/test_ticket_sla.py` exercises every one of them, because
a call site that forgot the helper is precisely what a test of the read routes cannot see.

**What Phase Q deliberately does not do.** §31's "SLA risks" and §28's `GET /analytics/sla`
are Phase S's: both need the deadline arithmetic expressed in SQL to sort and paginate by it,
and a second implementation of the clock that agrees with the pure function until one of them
is edited is the failure this whole design is arranged to avoid. §3's matrix gives `SLA_VIEW`
to admin, manager and agent and not to customer, so a portal caller's `sla` is `null` and the
timeline hides its SLA entries — two absences a client cannot distinguish from "this priority
has no active policy", which is deliberate. And ADR-023's closing debt is **still open**: a
notification whose five delivery attempts are exhausted leaves its row unstamped, and Phase Q
did not write the sweep that picks those up. It is a smaller debt than it was — the row and
the badge are the feature, and the email is a second way to learn about one — but it is not
closed, and this paragraph is where that is admitted.

**Cost.** A scheduled job is a new class of thing to operate: exactly one beat process (two
would double every sweep), a queue that a worker must be told about, and a judgement — the
sweep does not alert on a terminal ticket even when it is overdue — that lives in
`sla_repository.find_pending`'s `status` filter rather than in the clock. Five code comments
that named this phase are now paid, and the two enums that were waiting for producers have
them.

---

## ADR-025 — The socket authenticates in a frame, and the audience is decided by one predicate

**Status:** accepted · Phase R

**Context.** §25 gives real-time updates as a requirement: "ticket updates, new messages,
assignment changes, SLA warnings, AI analysis completion". §7 says "no critical state in local
process memory" and that "WebSocket fan-out [is] designed for multi-instance deployment from the
start". §54's checklist asks for "WebSocket subscriptions" among the cross-tenant probes. Phase R
is the phase five earlier places were left waiting for, each in writing: `Notification`'s
docstring says *"the websocket is the delivery optimization; this table is the source of truth"*;
the README carried "No WebSocket push" as a known limitation with the note that the row is
persisted "so that Phase R has something to push"; `docs/architecture.md` §8 was titled
**Real-time flow** and written as a plan, naming the channel, the ordering rule, and the reason
for Redis; and the frontend already held up its end — `vite.config.ts` proxies `/ws` to
`ws://localhost:8000` and rewrites nothing, and `App.tsx` sets `refetchOnWindowFocus: false` with
the comment *"WebSocket events drive invalidation"*. The path and the thin-envelope contract were
agreed before this backend existed on the other side of them.

**Decision 1 — the credential arrives in a first-message frame.** A browser cannot set an
`Authorization` header on a WebSocket handshake, so the three candidates were a query string,
`Sec-WebSocket-Protocol`, and a frame sent after `accept()`. The query string is the one every
tutorial uses and it is the wrong one here: uvicorn logs the full request line, so a live access
token would reach stdout on **every connect and every reconnect**, and
`tests/security/test_log_hygiene.py` **would not have caught it** — that suite records structlog
calls, so a token printed by the ASGI server's own logger is outside everything it can see. §4's
"no tokens in logs" is a requirement about logs, not about structlog, and a mechanism whose
compliance depends on a logger we do not control is not compliance. The subprotocol alternative
loses for a smaller reason: it abuses a negotiation header to carry a credential, and a server
that echoed the chosen protocol back would be putting the token in a response header. The frame
wins because it puts the credential in a message body — the one place a request line never
carries — and `docs/architecture.md:202` already said "connections authenticate **before
joining**", so this is the code matching a document written eight phases earlier.

The frame costs a handshake that completes before the caller is known, and that has consequences
which are decisions in their own right:

* **The refusal path is `close(code)`, not the §42 envelope.** `app/main.py`'s four exception
  handlers render `JSONResponse`, and a WebSocket scope has no response to render into. So the
  handler catches `AppError` itself and closes with a code the client can act on, defined in
  `app/websocket/events.py`: **4401** unauthenticated (also the answer to a malformed frame),
  **4403** authenticated but not permitted, **4408** nothing sent within `WS_AUTH_TIMEOUT_SECONDS`,
  and **1013** slow consumer — the one standard code in the list, with the other three following
  the convention every WebSocket library documents rather than claiming a meaning RFC 6455 already
  assigned.

* **`WS_AUTH_TIMEOUT_SECONDS` exists because an unauthenticated socket is a resource.** A peer that
  connects and says nothing holds a file descriptor and a task indefinitely. The timeout is the
  bound, and the test suite sets it to one second, which is the pattern the rate-limit ceilings
  already use.

**Decision 2 — the five checks are extracted, not copied.** `app/api/deps.py` already had the
authorization order written down in prose: decode, check the token type, load the user, check they
are active, and check their organization is not suspended. A socket must apply the same five in
the same order, and the tempting move is a second function beside the first — which would be two
implementations of "is this caller who they claim" that agree until one of them is edited.
`authenticate_token(db, token) -> User` is that sequence over a raw string, and `get_current_user`
becomes a two-line wrapper that extracts the bearer token and calls it. `tenant_context_for(user)
-> TenantContext` is the second extraction, kept here so that this module remains the only place a
`TenantContext` is constructed — a property the code already relied on and which a socket building
its own would have quietly broken. Behaviour is unchanged, and the evidence is that **the existing
auth suites were not edited**.

**Decision 3 — the envelope is thin, and the client re-reads.** A `ticket.*` envelope carries
`type`, `ticket_id`, `ticket_number`, the field that changed with both of its ends, and the three
facts the audience decision needs. It never carries a rendered `TicketRead`. Pushing one would
make `app/websocket/events.py` a second serializer of the same row, with its own row-scope and
permission decisions to keep in step with `app/api/tickets.py` — and the failure mode is a socket
whose payload disagrees with `GET /tickets/{id}`, which no client can detect and no test would
think to look for. This is the stand Phase Q took when it refused to re-express the SLA clock in
SQL, and the one `notification_service` takes when it refuses to parse `from_value` back into
structured data. The **one** exception is `NotificationEnvelope.title`, carried because a toast
wants something to render and its only alternative read is fetching a page; it is server-written,
immutable, and already exposed by `GET /notifications`.

**Decision 4 — one channel per organization, and a channel is not a key.** The channel name is
`org:{organization_id}`, which is not invented here: `docs/architecture.md` §8 writes the flow out
that way, and the code matches the document rather than the document being edited to match the
code. **Redis does not namespace pub/sub channels by database.** Keys are separated across db 0/1/2
(ADR-022), but a `PUBLISH` on db 0 reaches a `SUBSCRIBE` on db 1, so a running development server
and the test suite, pointed at the same Redis, share one channel space. That is worth stating
plainly because it looks like a bug the first time someone meets it, and because of what follows
from it: **isolation does not rest on the database number.** It rests on the envelope's
`organization_id` and on the registry being keyed by organization, which is a stronger arrangement
— a mechanism that holds regardless of how a client reached Redis is one that cannot be defeated by
a connection string.

**Decision 5 — the audience is one pure predicate with two axes, and the second axis is a bug that
would have shipped.** `visible_to(envelope, context)` takes no session, no `await`, and no socket.
The first axis is `TICKET_SCOPE_BY_ROLE` through `row_scope_for` — the same table that governs every
HTTP read, so the socket cannot drift from the routes because there is one table. The second axis is
internal content, and **row scope alone is not sufficient**: a customer owns their own ticket, so
scope alone would have pushed them `ticket.note_added` for the internal note written *about them*.
`MESSAGE_READ_INTERNAL` already answers this — it is held by staff and not by customers, and it is
applied in the service on the HTTP side precisely because a route cannot express "this field, for
this audience". So the envelope carries `internal: bool`, set by the producer from
`message.is_internal`, and the predicate requires the capability when it is set. The division is the
one `notify_for_event` already follows: **the publisher states the fact and the boundary decides the
audience.** Organization equality is checked a third time inside the predicate even though the
channel already guaranteed it, so that the boundary is fail-closed on its own rather than depending
on the transport having done its job — a boundary that only holds because of something else moves
the day that something else changes.

**Decision 6 — one slow socket must not delay anyone else.** The subscriber task is the only thing
on the instance turning Redis messages into socket writes, for every tenant on it. If it could block
on one socket — a suspended browser tab, a laptop that went to sleep, a peer that has stopped
acknowledging — then every other tenant's events would queue behind it. That is a cross-tenant
denial of service in a multi-tenant product, and it is why the fan-out is `put_nowait` into a
**bounded** per-connection queue, with each connection owning its own writer task, and why overflow
or a send timeout closes that one connection with 1013.

**A real bug was found here, by the test written to cover it, and it is recorded because the
mechanism is easy to get wrong twice.** The first `_drop_backlog` freed one slot and appended the
close marker. Against a queue filled to its 64-deep bound that left 63 stale envelopes *in front of*
the marker, and the client this path exists for is by definition not reading — so the writer would
block on the first of those and the close would never be sent until the send timeout fired ten
seconds later, leaving the connection registered the whole time. Emptying the queue is what makes
the marker the next thing the writer sees. It is a one-line difference between a design that works
and a design that appears to work, which is the argument for testing the decision where it is made
rather than only through a socket: reaching this state end to end means filling kernel buffers
before the application queue can start to fill, which is machine-dependent.

**Decision 7 — the local registry is process memory, and that is allowed.** requirements.md §7
forbids *critical* session state in local memory. A subscription registry is not critical: the
durable record is the database, the socket carries only notification that the record changed, and a
lost connection costs latency and nothing else. The requirement's second half is satisfied literally
— fan-out travels through Redis, so any instance serves any client with no sticky sessions and no
cross-instance coordination. What it costs is stated in Decision 8.

**Decision 8 — the worker exposes a Redis bug that nothing had hit yet, and the fix is a scoped
client.** Every Celery task reaches async code through `event_loop.run`, which builds **and closes**
a loop per call (ADR-011). A client built inside one invocation and cached in `app/core/redis.py`'s
module-level `_shared` is bound to a loop that is then closed — so the *second* sweep would publish
through a dead client, and the first would look fine. Nothing had hit this because no task had used
the async client before; the SLA sweep is the first worker-side publisher. `redis.scoped_client()`
builds and closes its own, and the publish functions take `client=None`, which is the shape
`RateLimiter.__init__` already uses for the same reason. This is exactly the class of bug a
phase-boundary review catches and a per-phase test suite cannot: it is not in any one component, it
is in the assumption they share.

**Decision 9 — no application-level ping.** uvicorn's protocol-level ping/pong
(`--ws-ping-interval`, 20 seconds) already detects a dead peer in both directions, and the handler's
read loop is what observes the resulting disconnect. An application heartbeat would be a second
liveness mechanism for a problem already solved one layer down, with its own timeout to tune and its
own way of disagreeing with the first.

**Decision 10 — §54's route walk learns about sockets, because it could not see one.** The walk that
proves every route is either public by declaration or guarded by a capability uses
`isinstance(route, APIRoute)`. `APIWebSocketRoute` and `APIRoute` are **siblings**, not parent and
child, so the first socket in this project would have been silently skipped — and
`test_the_walk_finds_every_documented_route` would not have noticed, because WebSocket routes do not
appear in the OpenAPI schema either. Two independent blind spots for the same route. The walk now
collects `APIWebSocketRoute` too, and `WEBSOCKET_ROUTES` joins `PUBLIC_ROUTES` as a list of its own,
with a docstring saying why: the socket's credential arrives *after* the handshake, so the per-route
capability dependency the other two allowlists describe does not apply to it, and what replaces it
is `tests/security/test_websocket.py`. Leaving the socket invisible to the mechanical guard and
asserting it by hand would have made §54's checklist true by inspection rather than by construction.

**Decision 11 — the socket declares the capabilities its contents need.** The handler checks
`TICKET_VIEW` and `NOTIFICATION_LIST` before registering a connection, and closes with 4403 without
them. All four roles hold both today, so this is a statement of what the channel carries rather than
a narrowing. It is worth the two lines because it is the only mechanism that would catch a future
role that lost either: without it, such a role would hold a socket that pushed it events it has no
route to read the details of.

**Decision 12 — a notification event is filtered per user, not addressed to a per-user channel.**
`docs/architecture.md` §8 sketched the alternative, and this is the one correction the implementation
makes to it. A channel per person would multiply the subscription set by the tenant's headcount to
buy a filter that is one comparison — and it would move the audience decision into the
*subscription*, where a connection's channel list becomes a second authorization surface to get
right. Filtering on the organization's channel keeps the decision in the predicate beside the other
one. `notify_for_event` and `notify_sla_alert` already decided *who* each notification is for, and
that decision is durable in the row; `notification_visible_to` compares the addressee and
deliberately does **not** re-derive it. Recomputing the audience here would be a second
implementation of the notification policy, agreeing with the first until one of them changed, and
its failure mode is a notification that appears in the inbox and not on the socket, or the reverse.

**Decision 13 — `_NOTHING_PUBLISHED` is a named set, not an omission.** `TicketEventType` has three
members that produce no `ticket.*` envelope: the SLA pair, because they change a clock rather than a
rendered field and they do notify somebody (as `notification.created`), and
`AI_ANALYSIS_COMPLETED`, because §25 names it and nothing produces it yet. A unit test asserts the
mapping and this set **partition** the enum, so a later phase that adds a timeline entry has to
decide whether it reaches a client — rather than having it silently never published. That is the
same guard `SILENT_EVENT_TYPES` and `DEFERRED_EVENT_TYPES` provide from the notification side, and
the entry naming `AI_ANALYSIS_COMPLETED` is a marker for Phase T-W to delete.

**What Phase R deliberately does not do.** No replay or backfill: a client that was disconnected
misses events and must refetch, which is the same contract the thin envelope already implies. No
inbound verb beyond the auth frame — a client cannot subscribe, unsubscribe, or acknowledge, because
nothing in §25 asks for it and every inbound verb is a new authorization surface. No per-connection
inbound rate limit, which the README records. No per-user channel (Decision 12). No analytics (§31's
SLA risks and §28's `GET /analytics/sla` are Phase S's). And no new dependency, no new process, and
no migration: `websockets==17.1` was already installed as a `uvicorn[standard]` extra, the subscriber
is a task inside the API's own lifespan, and **`alembic check` reports no new upgrade operations** —
the mechanical proof that the phase which added the most moving parts of any so far stores nothing
at all.

**Cost.** Three settings to operate (`WS_AUTH_TIMEOUT_SECONDS`, `WS_QUEUE_MAX_DEPTH`,
`WS_SEND_TIMEOUT_SECONDS`), a fan-out whose delivery is best-effort and at-most-once where the
notification row is durable, and a registry that is process memory — so a restart loses subscriptions
and every client reconnects, which is the trade Decision 7 accepts and names. The publish is
deliberately non-raising: a Redis outage must not fail a request that has already committed, so the
failure is a log line naming the error type and not the URL (a broker URL can carry a password) and
clients that do not reconnect never learn anything changed. And the subscriber reconnects with a
bounded backoff, so a Redis restart costs real-time delivery for the duration rather than for the
life of the process — a distinction that is invisible until the day it matters.

## ADR-026 — Analytics are read-through aggregates, and one SQL fragment is pinned by a differential test

**Status:** accepted · Phase S

**Context.** §28 lists the metrics, §31 asks for "SLA risks", §15 asks for dashboard caching and
requirements.md §7 restates it: *"Cache expensive dashboard results in Redis. Invalidate or expire
caches appropriately."* Phase S is the phase **four earlier places wrote down and deliberately did
not build**, each in writing. The SLA router's own docstring names it: *"The queue-wide view —
§31's 'SLA risks' and §28's `GET /analytics/sla` — is not here either. Both need the deadline
arithmetic expressed in SQL to sort and paginate by it, and a second implementation of the clock
that agrees with the pure one until one of them is edited is the specific failure this phase is
arranged to avoid. **Both arrive in Phase S**."* ADR-022's Redis responsibilities list carries
*"**Cache → Phase S.** §15's own example is dashboard analytics."* `docs/architecture.md` §10 has
described the caching design in the present tense since Phase B while nothing cached. And
`Permission.ANALYTICS_ORG` and `Permission.ANALYTICS_OWN` have sat in the matrix unused since Phase
G, transcribing §3's two Analytics rows.

**Decision 1 — the deadline arithmetic is one SQL fragment, and the differential test is what keeps
it honest.** Ranking "which open tickets are nearest a deadline" cannot be done in Python without
either reading every open ticket (§7 forbids an unbounded query) or discarding the ranking. So
`analytics_repository` gains one expression — the due instant of each *unstopped* timer, as
`created_at + make_interval(0, 0, 0, 0, 0, target_minutes)` joined to the ticket's active policy —
used for ordering, for the past-due count, and for compliance.

**Every number a client sees still comes from `resolve_position`.** The SQL decides *which* tickets
and *in what order*; the clock decides *what is true about them*. The failure mode of a disagreement
is therefore a wrong position in a list and never a wrong number on a screen. The test that replaces
"one implementation" is `tests/integration/test_analytics_sla_agreement.py`, which builds tickets
straddling every boundary — a second either side of the warning instant and of the due instant, both
timers, met and breached and unstopped, plus a ticket whose priority has no active policy — and
asserts the SQL classification and the clock agree **row for row**, in both directions. It runs
against a fixed `NOW` rather than the wall clock, and it earned its keep on its first run by catching
a real defect: `overdue_count` compared `< now` while `resolve_timer`'s second branch is
`now >= due_at`, so a ticket the countdown calls breached was one instant short of counting as past
due. **That is the class of defect the file exists to catch, and it was found before anyone saw it.**

Two details are decisions in their own right:

* **`LEAST` is what lets one expression serve both timers.** PostgreSQL's `LEAST` ignores NULLs, so
  `LEAST(response_due_at IF first_response_at IS NULL, resolution_due_at IF resolved_at IS NULL)` is
  the soonest deadline that has not been met, and NULL — and therefore excluded — when both have. The
  three-valued-logic trap `scoping.py` warns about does not arise because the NULL case is not
  matched, and the differential test asserts that rather than assuming it.
* **The warning instant is deliberately *not* in SQL.** `_warning_offset` multiplies a `timedelta` by
  a float, so expressing it in SQL means integer minutes and a possible few-seconds disagreement at
  the band edge — the exact class of near-miss the differential test exists to catch, introduced on
  purpose would be worse. So the cached half counts what needs only `due_at` and `now`, and anything
  depending on the warning band comes from the clock, in the un-cached risk list.

**Decision 2 — the cache key carries the row scope, and that is the phase's second isolation
boundary.** Analytics are tenant-scoped *and* row-scoped: the same endpoint answers a manager with
the organization's totals and an agent with their own. A key of `(organization, metric, range)` would
be correct in every cross-tenant test and would still serve an administrator's organization-wide
payload to an agent who asked the same route in the same second — a leak **inside** one tenant, which
no cross-tenant assertion can see, because both callers are in the same organization and the query
that filled the entry was correctly scoped when it ran. So `cache_key` embeds `scope_token(context)`:
`org`, or `user:{id}` for an `ASSIGNED` caller, derived from `TICKET_SCOPE_BY_ROLE` rather than from
a role name so a key cannot disagree with the query that fills it.
`tests/security/test_analytics_isolation.py` asserts the consequence three ways — the second
caller's number is their own, Redis holds an entry under the organization token, and it holds a
*different* entry under the agent's — and the `customer:` branch is asserted unreachable rather than
left as a comment somebody later decides is dead code.

**Decision 3 — invalidation is a version integer, and the orphan is its cost.** Redis has no way to
delete a pattern of keys without `SCAN`, and `KEYS` blocks the server and is banned in production. So
a write does not chase the entries it invalidated: it `INCR`s `analytics:version:{organization_id}`,
and every key embeds that number, so one increment makes every existing entry unreachable at once.
The old entries are **orphaned rather than deleted** — they sit until their TTL expires, bounded by
it, at one `INCR` per write and no scanning.

**The version key deliberately has no expiry.** A version that could expire would reset to zero, and
a `...:0:...` key written after the reset could collide with a version-0 entry that had not yet
expired, resurrecting a stale payload. Without a TTL the version only ever moves forward. It is one
small integer per tenant.

**There are eight call sites, and the eighth is not a ticket.** Seven are the ticket writers, each
one line beside the existing post-commit realtime publish — the same place and the same moment:
`create_ticket`, `assign_ticket`, `change_priority`, `change_status`, `close_ticket`,
`reopen_ticket`, and `post_reply` (the first public reply stops the response timer, which moves the
compliance number). The eighth is `sla_service.update_policy`, and it is the only one that is not a
ticket write. **Every SLA number in the cache is computed by joining `tickets` to that row:** a
target moved, or `is_active` switched off, changes which tickets have a deadline at all — arithmetic
that lives in the join condition rather than on any ticket column, so no ticket write would ever move
it. Without that line a manager could widen a target and watch the compliance rate stay wrong for a
full TTL. The plan listed seven; the eighth was added because the join is an input to every
aggregate, and it is named here so a reader can see it was a decision rather than a stray edit.

**Decision 4 — every path fails open, and only the error's *type* is logged.** Like
`realtime.publish` and `notification_service.enqueue_delivery`, a Redis outage means "compute it",
never a failed request: §15's cache is an optimization, and an optimization that can take a dashboard
down is a regression. `invalidate` failing open means the version does not move and a stale entry can
outlive the outage by up to one TTL — **stated in the README rather than hidden, and since
measured**: with Redis stopped, the endpoint answered correctly and a ticket written during the
outage landed, but the version stayed where it was, so a pre-outage entry was served — total 2 where
the truth was 3 — until it expired 31 seconds later, at which point the same window read the truth.
The log line names the error type and never the message, because a `ConnectionError`'s message embeds
the address it failed to reach and `REDIS_URL` carries a password in production — the reasoning
`app/core/redis.py` already records.

**Decision 5 — the cached payload is validated on the way back out, and a payload that does not parse
is a *miss*.** The same stance `app/websocket/events.py` takes on a broker message: "validate all
input" applies to a Redis value as much as to a request body. `redis` is not a trusted store — a
value could have been written by a deploy with a different response shape, by a hand-run `redis-cli`,
or by an older version of the process. Treating it as a miss rather than an error is what makes a
deploy that changes a response shape safe instead of a source of `500`s.

**Decision 6 — the SLA aggregate is cached; the risk list is not.** A cached countdown would be a
wrong countdown: `remaining_seconds` is a function of `now`, and a manager reading "40 minutes left"
an hour after a breach is worse than no dashboard. So `/analytics/sla` composes a cached aggregate
block with a live ranked list, and `SLAStanding` is a model of its own precisely because it is the
unit that gets cached — caching an `AnalyticsSLA` with `risks` empty and patching the list in
afterwards would be an entry that says it is a response and is not one. §15's "do not cache
everything blindly" is asking for exactly this distinction. The two halves also answer about
different populations, which is worth knowing before comparing them: compliance describes tickets
**created in `range`** — the cohort reading, so the rate and the volume chart describe the same
tickets — while `overdue` and `open_tickets` describe the queue **as it stands**, whatever the
window says. A ticket raised before the window and still past due appears in the second pair and not
the first, which is the reading a manager wants; hiding the worst tickets because they are old would
be the worst possible omission.

**Decision 7 — row scope does the work, and `ANALYTICS_ORG` guards exactly one route.** §3 has two
Analytics rows and `ROLE_PERMISSIONS` already transcribes them. So `/overview`, `/tickets`, `/sla`,
and `/sentiment` require `ANALYTICS_OWN` — the capability all three staff roles hold — and narrow by
`row_scope_for(role, TICKET_SCOPE_BY_ROLE)`, the same map every ticket read uses. **The same endpoint
answers differently by role, exactly like `GET /tickets`, and no second scope map exists** to agree
with the first until one of them is edited. `/agents` alone requires `ANALYTICS_ORG`, because a
per-agent breakdown is a comparison between people, which is §3's "org-wide analytics"; an agent's
own performance is what the other four routes already return for them. A customer holds neither
capability and is refused with a `403` before the service is reached, so no cache key is built under
a customer's scope.

**Decision 8 — the AI metrics are real queries over columns Phase U will fill.**
`/analytics/sentiment` and `ai_usage` return zeros and one `None` bucket, truthfully. §8's eleventh
criterion is that analytics come from real aggregation queries and never hardcoded values, and a real
`COUNT` over a table nothing writes yet is a real query answering the question actually asked. The
response shapes are final, so Phase U populates them with no API change — the same reasoning
`app/schemas/ticket.py` already records for the ticket AI fields. `cost_usd` is a `Decimal` and
renders as a decimal string, for the reason `app/models/ai_usage.py` gives about its own column: it
is money and it gets summed, and a float would put binary rounding into an invoice. A zero sum
renders as `"0"` rather than `"0.000000"`, because Postgres renders the scale from the value.

**Decision 9 — no new index, therefore no migration, and no rollup table.** The aggregates group by
`status`, `priority`, `category`, and `assigned_agent_id` and filter by `organization_id` and
`created_at` — every one of which Phase D already indexed for these exact predicates — and `ai_usage`
carries its own pair. **`alembic check` reporting "no new upgrade operations" is the mechanical proof
that Phase S adds no table, no column, and no index.** No materialized view or rollup was built: a
rollup is a schema change plus a staleness rule, and nothing has measured these queries as slow. §53's
instruction is *"Do not prematurely optimize everything."*

**What Phase S deliberately does not do.** No frontend dashboards. The spec's Phase S line says
"Build frontend dashboards", and the frontend stays at Phase C scaffolding as it has for every phase
— §8's first success criterion is that *"the backend runs and is fully exercisable without the
frontend"*, which `scripts/phase_s_walkthrough.py` demonstrates. This is a deliberate deferral,
recorded here rather than left implicit. No `report_tasks.py` and no scheduled or exported reports:
§16's "report generation" is not a phase assignment, and a scheduled report needs a schedule, a
delivery path, and an artifact store, none of which this phase has. No Celery task, because the
caching §15 asks for is read-through rather than a rollup. No `EXPLAIN`-driven index work: if the
walkthrough's numbers look wrong the index question reopens, and the indexes Phase D built for these
predicates are the reason to expect it will not. And no per-tenant timezone: buckets are UTC days
because no organization carries a timezone, and inventing a setting nothing sets would be worse than
the documented limitation.

**Cost.** One setting to operate (`ANALYTICS_CACHE_TTL_SECONDS`, 300 by default), one Redis keyspace
that grows with the number of distinct windows queried, and orphaned entries that live out a TTL —
bounded, but present in every `KEYS` a developer runs. The risk list is a **ranking, not a page**:
`limit` defaults to 10 and caps at 50, with no `offset`, because the eleventh-worst ticket is not a
dashboard's business — which means a tenant with 400 tickets past due sees 50. The `risks` half of
`/analytics/sla` is computed on every request, so the safest query in the phase is not the cheapest.
And `/analytics/tickets` and `/analytics/sentiment` are deliberately uncached, which trades a little
CPU for not showing a stale count on the one chart somebody refreshes the moment they assign work.

**Verification.** Three comparisons in `analytics_repository.py` were each inverted in turn —
compliance's `<=` on `stopped_at <= due_at`, `overdue_count`'s `<=` on the deadline, and the NULL
guard that keeps `LEAST` from seeing a stopped timer's deadline — and the differential test failed on
the comparison it pins and on no other: "2 failed, 9 passed", "1 failed, 10 passed", and "2 failed,
9 passed". A test that pins two implementations together and cannot fail is not pinning them. The
whole suite is 1092 tests; `scripts/phase_s_walkthrough.py` runs 58 assertions live against a fixture
built through the API, every expectation derived from the fixture's own ages and the tenant's own
policies; and the fail-open path was checked with Redis actually stopped, which is also how the
stale-window cost above was measured rather than estimated.

## ADR-027 — The AI layer is one provider boundary and one call path, and every attempt reaches the ledger

**Status:** accepted · Phase T

**Context.** §17 states the whole of this phase: *"Create AI provider abstraction. Implement:
configuration, provider client, structured output validation, timeout handling, retry strategy,
usage tracking."* Four earlier phases wrote it down and left the seams for it, each in writing.
Phase D built `ai_usage` and `ai_analyses` with their constraints and indexes and **nothing has
ever written a row**. Phase S pre-built the read: `GET /analytics/overview` already returns an
`ai_usage` block, with `failed_calls` counted separately *"because `ai_usage` records a failed call
— it consumed quota and may have been billed"*, and ADR-026 Decision 8 defended the zeros as *"a
real `COUNT` over a table nothing writes yet is a real query answering the question actually
asked."* Phase C wrote the `--- AI ---` block into `.env.example` and then deliberately left it out
of `config.py`, because of the rule that file repeats: *a setting with no consumer is a guess with a
name.* And Phase G transcribed §3's AI permission rows — `AI_REQUEST_ANALYSIS`,
`AI_REQUEST_SUGGESTION`, `AI_QUERY_KNOWLEDGE`, `AI_CONFIGURE`, `AI_VIEW_USAGE` — which guard nothing
yet.

ADR-008 had already made the load-bearing choice in Phase C: *"Implement `AIProvider` with Claude
behind it for classification, sentiment, summarization, and drafting, using tool-use for structured
output… A deterministic fake provider exists for tests only."* The vendor was settled. Every
mechanism below it was not.

**Decision 1 — the protocol carries the four generation operations, and `generate_embedding` is
deliberately absent.** §17 lists five methods, and the plan for this phase restated all five.
ADR-008 puts embeddings behind the *same interface* with a **different vendor** and defers that
choice to Phase X. Declaring the method now would force `ClaudeProvider` to carry one it can never
serve — a stub with no caller, which is what this codebase refuses everywhere else: settings wait
for their consumers, and `celery_app.py` still does not declare the `ai` queue because *"declaring a
queue nothing publishes to is a worker process waiting for work that does not exist."* So Phase X
adds `generate_embedding` together with the provider that can answer it. **This is a deviation from
§17's literal list, recorded rather than quietly taken**, and `provider.py`'s docstring says so at
the place a reader would look for the missing method.

**Decision 2 — validation is one function, so §18 is structural rather than per-vendor.** §18:
*"The backend must validate the returned structure. Never assume LLM output is automatically
valid."* A guarantee implemented once per vendor is a guarantee with as many implementations as
there are vendors, so `validate_output` in `app/ai/provider.py` is the single place a payload
becomes a Pydantic model, and **both** `ClaudeProvider` and `FakeProvider` call it. Each provider's
job is reduced to getting a payload out of its own SDK; the malformed-output test therefore needs no
vendor at all, and a third provider inherits the §18 behaviour by using the function.

Every shape of rejection produces the same `AIOutputError`: prose instead of a tool call, a tool
call naming the wrong tool, arguments that were never closed because the answer hit `max_tokens`,
JSON that is not an object, an object missing a field, a confidence of `1.4`, a sentiment the model
invented. They are one condition for a caller — the answer could not be trusted — and the reason
string distinguishes them for the log. **The log never quotes the payload.** `ValidationError`
carries the offending `input` beside each failure, and for these schemas that input is
model-authored text derived from a customer's message, so `_failure_summary` reports only `type` and
`loc`, capped at five failures: an uncapped string would put kilobytes in a log line for no extra
information.

**Decision 3 — the retry policy lives in one place, so the SDK's own retries are off.**
`AsyncAnthropic(..., max_retries=0, timeout=AI_TIMEOUT_SECONDS)`. A provider that also retried would
multiply two policies, and the attempt count in the ledger would stop meaning anything: three SDK
retries behind one application attempt would be recorded as one call. The timeout is the client's
because the SDK owns the socket; everything about *what to do when it fires* is `ai_service`'s,
because that is policy.

**Only `AITransientError` is retried** — unreachable, timed out, throttled, 5xx — up to
`AI_MAX_ATTEMPTS` (3), exponential from `AI_RETRY_BACKOFF_SECONDS` with a delay jittered between
half and all of the ceiling. **Full jitter would be the textbook choice and is wrong here**: it can
return a delay near zero, and the failure being retried is usually a rate limit, which is the one
case where waiting less is pointless. `AIPermanentError` (a refused key, an unknown model) fails on
the first attempt, and so does `AIOutputError`: the same input at the same temperature reproduces
the same unusable answer, §53 names *"repeated AI calls"* as waste, and paying twice for one
malformed answer is exactly that. A truncation is fixed by raising `AI_MAX_TOKENS`, which is a
configuration answer and not a retry one. `_sleep` is bound at module scope so the tests replace it
— retry timing is worth testing, and a suite that slept through backoff would be the slowest thing
in the run.

**Decision 4 — every attempt is recorded, including the ones that fail, and the caller commits.**
`AIUsage`'s own docstring is the rule: *"A failed call still consumed quota and may still have been
billed, so it is recorded rather than dropped."* So `_run` stages one row per attempt — the
transient failures on the way to a success, the permanent failure, the malformed answer, and the
success — which is why a retried call appears three times and why `failed_calls` is a subset of
`calls` rather than a complement. **Nothing here commits.** `ai_service` is called from inside a
transaction a route or a task already owns, and committing would end it early; so the obligation
travels with the call, and a caller that lets its own rollback discard these rows loses exactly the
record that matters most. Phase T writes no caller, so the obligation is discharged by
`scripts/phase_t_walkthrough.py`, which commits after a deliberate permanent failure and then reads
`failed_calls: 1` back over HTTP rather than asserting that it happened.

**A related obligation has no discharge in this phase, and it belongs to Phase U.** An `ai_usage`
write does not bump `analytics:version:{organization_id}`, so `/analytics/overview`'s AI block can
read up to one TTL stale after a call. `ai_service` must not invalidate — invalidating before the
caller's commit lands is the race `ticket_service` comments about at its own invalidation — so
**whoever commits owns the invalidation**, in the same place and at the same moment as the
post-commit realtime publish. It is recorded here as a debt with a named owner rather than left as
an oversight, and it is why `tests/integration/test_ai_usage_ledger.py` reads `overview` at most
once per tenant per window: a second read of the same key answers from Redis, or recomputes if Redis
is down, and an assertion that passed or failed depending on whether a container was running would
be worse than no assertion.

**Decision 5 — cost is computed at write time, from a table `AI_MODEL` is validated against.**
`AIUsage.cost_usd` exists because *rates change and a historical row must keep the price actually
charged*, so the price is computed when the row is written and never recomputed on read.
`app/ai/pricing.py` holds published rates per model with the source and the date it was read in the
module docstring, and `Settings` **refuses an `AI_MODEL` that is not in that table**. The
alternative is a deployment quietly logging `cost_usd = 0` forever: a wrong number, which is worse
than a missing one, and the same instinct that made `scoping.py` a required argument rather than a
default.

**Decision 6 — a rejected answer is priced from the tokens it was billed, and that took a fix.**
Found while writing this phase's tests: `validate_output`'s `AIOutputError` carried no token counts,
so the most expensive failure there is — a full-length answer that was then rejected for not
matching the schema — would reach the ledger as a zero-cost row. `claude.py` now catches the
rejection and re-raises it with the counts it read off the response, which is why the counts ride on
the exception at all: the exception is the only thing that leaves the provider on that path, and a
caller that had to ask a second time would be asking about a call that has already ended. The
regression is pinned by a unit test that drives the real `ClaudeProvider` against a fake SDK client
returning a `1.4` confidence and asserts the row's cost equals `cost_usd` for the tokens it was
billed.

**Decision 7 — the fake provider is refused outside the test environment.** §60 forbids *"use fake
AI results in the final implementation"* and ADR-008 says the fake exists for tests only. A
convention would not hold that line; a validator does. `AI_PROVIDER: Literal["anthropic", "fake"]`
plus a validator that raises when `AI_PROVIDER == "fake"` and `ENVIRONMENT != "test"`, and
`tests/unit/test_config.py` asserts the refusal rather than trusting it. The fake is also scripted
rather than generative: it answers from a queue of outcomes and counts calls, and a test that forgets
to script it gets an `AssertionError` naming the call count instead of a plausible-looking result.
`ai_service._provider` is the seam a test patches, which is how the retry tests script three
attempts without a vendor.

**Decision 8 — customer-written text is fenced before it reaches a model.**
`app/ai/prompts.py`'s `as_untrusted(label, text)` wraps the text in a delimited block under a
sentence saying it is content rather than instruction, and **defuses any fence marker appearing
inside it — in the body and in the label** — so the boundary cannot be spelled by the customer. The
provider applies it, not the caller, so fencing is not something a prompt author can forget. This is
mitigation and not a guarantee: prompt injection is unsolved, and the honest containment is the two
decisions above it — the answer must be a validated model, and no model output can send anything.

**Decision 9 — nothing is autonomous, and the types say so.** `SuggestedReply` has a body and no
confidence, no status, and no sender: the schema cannot express "sent". §21's *"AI must NEVER
automatically send a customer-facing response in the default implementation"* is enforced by what
the return type can say rather than by a comment a later caller could ignore.

**Decision 10 — no route, no queue, no `ai_analyses` rows, no migration.** §36's three AI routes —
`/ai/analyze`, `/ai/summarize`, `/ai/suggest-response` — belong to Phases U, V, and W, and this
phase builds no consumer for them. `celery_app.py` is unchanged for the reason Decision 1 gives.
`ai_analyses` is a ticket-scoped history of what the model said, and Phase U is what has something
to store; Phase T writes the ledger and nothing else. **`alembic check` reporting "No new upgrade
operations detected" is the mechanical proof that Phase T adds no table, no column, and no index**,
the same proof Phase S used.

**What Phase T deliberately does not do.** No embedding provider and no `generate_embedding`
(Decision 1, Phase X). No route, no Celery task, no `ai` queue (Decision 10). No `AIAnalysis` rows
(Phase U). No AI result cache: `was_cached` is written `False` on every row, and §20's *"avoid
regenerating"* is Phase V's, because writing `True` for a call that reached the provider would
corrupt the measurement the column exists for. No rate limit on AI calls: §45 names AI endpoints,
and the limit arrives with its consumer as every other limit did. No prompt-injection defence
beyond fencing: §24's grounding rules are RAG work. And no frontend, as in every phase — §8's first
success criterion is that the backend is fully exercisable without it.

**Cost.** One new runtime dependency (`anthropic`, pinned, and it ships `py.typed` so mypy `strict`
needs no override — unlike boto3 and Celery), and `httpx` as a dev-only one for the SDK's transport —
which was right for this phase and wrong for the next one: httpx is the transport the second vendor
uses directly, and the production image installs runtime dependencies only, so it moves to
`dependencies` in ADR-028 rather than shipping an image where a provider raises `ImportError`.
Seven settings to operate, all defaulted, and one of them (`AI_API_KEY`) is optional because every
self-hosted installation that does not want AI starts without a key — the failure is loud at the
point of use rather than at import, so a checkout with no key still runs the whole test suite. The
ledger grows at **one row per attempt, not per call**, so a rate-limited afternoon costs three rows
where a naive count would say one; that is the intended reading, and `calls` in the analytics block
counts attempts too. Nothing is saved yet, because nothing is cached. And the honest cost of
Decision 4 is that the ledger's completeness depends on callers honouring a commit obligation
nothing can check for them.

**Verification.** The four §46 cases this phase exists to cover were each deliberately broken and
the corresponding test confirmed to fail, because a test that cannot fail is not testing anything:
bounding the retry loop at one attempt failed five tests in `tests/unit/test_ai_retry.py`; replacing
`validate_output`'s `model_validate` with a cast to the raw payload failed fifteen in
`tests/unit/test_ai_structured_output.py`; removing `claude.py`'s token re-attachment failed
`test_a_rejected_answer_is_priced_from_the_tokens_it_was_billed` and nothing else; and logging
`str(exc)` instead of `type(exc).__name__` failed two in
`tests/security/test_ai_log_hygiene.py`. Each break was reverted and the files confirmed
byte-identical to their backups, which matters here because `app/ai/` is new in this phase and has
no earlier revision to restore from. The suite is 1209 tests, up from Phase S's 1092, all passing;
`alembic check` reports no new upgrade operations, which is the mechanical proof that this phase
adds no table, column, or index.

**The live end-to-end was outstanding when this phase closed, and ADR-028's follow-up discharged
it.** `scripts/phase_t_walkthrough.py` was written here and its refusal path was exercised — with
`AI_API_KEY` empty in `.env` the script prints what to add and exits `0`, and the API it needs
starts cleanly with the new module in place — but the four real calls and the dashboard read-back it
exists for had **not** been run, because the key this repository's `.env` is meant to hold was not in
it. What the script adds is the two things no test in the suite can show: that a real model answers
through `ai_service`, and that the ledger and the endpoint agree across a process boundary.
Everything it asserts about the ledger's contents is asserted against a real database in
`tests/integration/test_ai_usage_ledger.py`, and the endpoint's own behaviour is covered in
`tests/api/test_analytics.py` — so the outstanding part was the provider rather than the plumbing,
exactly as recorded here rather than described as done. It ran a day later against a second vendor;
ADR-028 carries the numbers.

## ADR-028 — A second provider proves the boundary, and the rate table decides which model belongs to which vendor

**Status:** accepted · Phase T follow-up

**Context.** ADR-027 ended with a claim and no way to check it. *"A provider-agnostic interface"* is
easy to assert when there is one real implementation, because a boundary with nothing on the other
side is indistinguishable from a straight line. `ClaudeProvider` and `FakeProvider` were both written
in the same phase by the same hand against the same assumptions, and the fake in particular was
written *to* the interface rather than discovered through it. So when a second vendor became worth
adding — a Groq key, free at the tier this project is deployed on — the question it actually answered
was whether Phase T built an abstraction or a wrapper.

Two things had to be true before any of it was written, and neither was.

**The credential was in a committed file.** `.env` held the key correctly, and `.env.example` held the
same live value — and `.gitignore` ends with `!.env.example`, so that file is tracked. `git status`
listed it as modified: committing anything, including this change, would have published the key to the
repository and to its history. `git log --all -S'gsk_'` and `git show HEAD:.env.example` both came
back empty, which is the check that matters: because no commit had ever contained it, removing it from
the working tree was sufficient and **no rotation was needed**. Had either returned a match, the key
would have had to be revoked at Groq regardless of what was done to the working tree, since deleting a
line does not delete a revision. It was removed with a regex substitution inside a process rather than
through a command line, so the value never appeared in a shell, a log, or a transcript. A template
file that ships in the repository carries `AI_API_KEY=` with no value; that is what it had before, and
what `tests/unit/test_config.py` describes.

**The configuration could not have worked.** `.env` had `AI_PROVIDER` unset — defaulting to
`anthropic` — with `AI_MODEL=claude-sonnet-5`, so the first real call would have sent a `gsk_` key to
Anthropic and been refused with a 401. The walkthrough would have ended at `failed_calls: 1` with zero
successful calls: correct behaviour for a wrong key, and the wrong outcome for a right one.

The key itself was confirmed live before any of this was planned around: `GET /openai/v1/models`
returned 200, and a tool-calling probe of `openai/gpt-oss-120b` returned `finish_reason: tool_calls`
with `arguments` parsing to exactly the shape `Classification` expects.

**Decision 1 — a second vendor is one module, and nothing above it changed.** This is the decision the
ADR exists to record, and the evidence is a diff rather than an argument. Adding Groq touched §17's
protocol: no. §18's `validate_output`: no. The retry policy, the jittered backoff, the ledger's
one-row-per-attempt rule, the error vocabulary, the fencing in `prompts.py`, `ai_service._run`: none of
them. What was added is `app/ai/groq.py`, a `_PROVIDERS` entry in `ai_service`, and a rate table row.

**The specific reason it cost nothing is worth stating, because it was not foresight.** An
OpenAI-compatible API returns tool arguments as a JSON **string** under
`tool_calls[0].function.arguments`, where the Anthropic SDK hands back a parsed object.
`validate_output` has accepted a string, a mapping, or an instance since Phase T — written that way so
`FakeProvider` could script an answer — and that tolerance is the whole of the compatibility. Had it
been written for the Anthropic shape alone, this change would have needed a `json.loads` in the new
module, which is §18 implemented a second time, which is the thing Decision 2 of ADR-027 forbids. A
test asserts the string genuinely reaches the parser rather than being pre-parsed on the way in.

**Decision 2 — the two schema helpers move to `provider.py`, so each vendor descends from one copy.**
`_inline` and `_tool_schema` were `claude.py`'s. Both vendors ask their model to answer by calling a
tool constrained by a JSON Schema, and they differ in how that schema is *enveloped* and in nothing
else — the envelope is the vendor's (`"input_schema"` for Anthropic, `"parameters"` inside
`{"type": "function", "function": …}` for Groq), the schema is not. Copying them would have produced
two schemas that agree today and drift at the next enum member, so the helpers were promoted rather
than duplicated. `provider.py`'s docstring says so at the place a reader would wonder why a
provider-agnostic module knows what a tool schema looks like.

**Decision 3 — the rate table gains a vendor column, and the pairing is refused at startup.**
`ModelRate` gained a leading `provider: str`; `rate_for`, `cost_usd`, `is_priced` and `priced_models`
kept their signatures, so every existing call site is untouched. `models_for(provider)` was added so
an error message can say what to do and not only what was wrong.

Then `Settings` grew a `model_validator` asserting `rate_for(AI_MODEL).provider == AI_PROVIDER`,
skipped when the provider is `fake` (whose model is not in the table at all).

**This is the check that would have caught the configuration described above, and it is why it
exists.** A rejected credential sends its reader to look at the credential — they go and check a key
that is perfectly good, regenerate it, and get the same 401. The mismatch is a fact about two settings
that were never compared, so it is refused where both are in scope and the message can name them:

    AI_MODEL='claude-sonnet-5' is served by 'anthropic', not by AI_PROVIDER='groq'.
    Models for 'groq': openai/gpt-oss-120b

`AI_API_KEY` stays a **single** setting rather than becoming one key per vendor, because it is the
credential *for the configured provider*: a second variable would be a setting one of the two
providers never reads, which is the rule this codebase applies everywhere else — a setting with no
consumer is a guess with a name. Its shape differs by vendor (`sk-ant-…`, `gsk_…`) and that is
documented in its comment and in `.env.example` rather than encoded as two fields.

The default stays `anthropic`. A default is what a deployment gets when nobody decides, and the
committed default should not be one machine's free-tier account.

**Decision 4 — Groq answers a malformed generation with HTTP 400, and that is an output error.**
`error.code == "tool_use_failed"` arrives with status 400, which the status table maps to
`AIPermanentError`. The honest reading is *"the model produced an answer that does not match the
schema"* — that is `AIOutputError`, and the distinction is not cosmetic: it is the difference between
the ledger saying *the vendor refused us* and *the model answered badly*, which are different
investigations. `_from_status` therefore checks the error code **before** the status code, and a test
pins the ordering, because a later refactor that reordered those two branches would look harmless.

The same body carries `failed_generation`: the model's own attempt, derived from the customer's
message we sent it. So the reason string comes from a **small allowlist of known `error.code` values**
— the same discipline as `_STATUS_REASONS` — and never from `error.message` or the raw body. §54's
"no tokens in logs" and §18's "model output is untrusted data" both apply to text we *receive*, not
only to text we send. The log-hygiene test constructs a 400 whose `failed_generation` is the
customer's subject and body and whose `message` contains the key, and asserts neither survives.

**Decision 5 — `reasoning_effort: "low"`, because a reasoning model's thinking is billed inside the
ceiling.** `gpt-oss-120b` spends reasoning tokens from the same `max_completion_tokens` budget as its
answer. At the default effort it can consume `AI_MAX_TOKENS` thinking and truncate before ever
emitting the tool call — producing `finish_reason: "length"`, which is the failure mode that looks
like a schema bug and is a configuration one. These are four narrow extraction tasks whose output is
already constrained by the tool schema; there is nothing to reason about at length. This is recorded
as a **vendor-specific tuning knob and not a general one**: it is set in `groq.py`'s request body, not
in `Settings`, because it is a property of this model rather than a policy of this application.

**Decision 6 — `Retry-After` is deliberately not honoured.** A 429 from a provider usually carries
one, and honouring it is the convention. It is refused here because it belongs to the *vendor's*
pacing and our retry budget belongs to the *caller's*: the whole sequence has to fit inside a request
a person is waiting on, and on a free tier `Retry-After` is routinely longer than all three attempts
combined. Blocking a support agent for sixty seconds is worse than failing fast and letting the caller
decide what to show. Same reasoning as ADR-027 Decision 3's rejection of full jitter: the textbook
answer is not the answer when a human is watching.

**Decision 7 — `httpx` moves from dev to runtime.** It was a dev-only pin in Phase T, which was right
then: the `anthropic` SDK pulled its own transport and httpx was there for tests. It is now the client
`groq.py` imports directly, and `backend/Dockerfile`'s production stage installs runtime dependencies
only — so leaving the pin where it was would have shipped an image where the Groq provider raises
`ImportError` on its first call, in production, on the one path a test could not reach. The dev entry
was deleted rather than duplicated, so the pin has one home. httpx ships `py.typed`, so mypy `strict`
needs no override — unlike boto3 and Celery.

**Decision 8 — `cost_usd` is a list price, and the free tier is why that has to be said out loud.**
`AIUsage`'s docstring said the column records *"the price actually charged"*, and on Groq's free tier
the price actually charged is zero — so either the column becomes uniformly zero, or it means
something else. It means the **provider's published rate for the model at write time**, and the
docstring now says so: a rate is a fact about the model, the invoice is a fact about the account, and
what the ledger needs is the first one. The alternative was a cost panel that reads $0.00 forever on
the plan a portfolio deployment actually runs on, which is exactly the *"hardcoded fake analytics"*
§60 forbids wearing better clothes. `openai/gpt-oss-120b` is in the table at its published
$0.15 / $0.60 per million tokens, read from Groq's model page with the date beside it, like every
Anthropic row.

**Decision 9 — the rejected answer is priced from the tokens it was billed, again.** This is ADR-027
Decision 6 repeated in a second module rather than shared, and it is worth saying why it is not an
abstraction. On the `AIOutputError` path the *exception* is the only thing that leaves the provider,
so the token counts have to ride on it; `_structured` catches the rejection, re-raises with the counts
it read off the response, and the ledger prices a full-length answer that was thrown away. The reason
it is written twice is that the two modules read tokens from two different response shapes and there
is no common place between the read and the raise. A test drives a `1.4` confidence through the real
`GroqProvider` and asserts the row's cost equals `cost_usd` for the tokens it was billed.

**What this deliberately does not do.** No new route, no Celery task, no `ai_analyses` row: §36's three
AI endpoints are still Phases U, V, and W, and this changes no behaviour a client can observe except
which vendor answers. No embedding provider: `EMBEDDING_MODEL` stays declared-and-unused for Phase X
per ADR-008. **No third provider** — two implementations is what makes the boundary real, and three
would be a plugin system nobody asked for. No rate limit on AI calls, per §45: still no endpoints. No
provider-agnostic abstraction over the *response formats*: each module reads its own vendor's JSON,
because a normalising layer would be a third schema that agrees with neither. And no `report_tasks.py`,
which remains unbuilt.

**Cost.** `httpx` promoted from dev to runtime, which is no new dependency at all — the pin only
changed sections. One rate-table row and one `provider` column on `ModelRate`. One configuration
validator, which is the first thing in this project that refuses a *combination* of settings rather
than a value. Two vendor-specific translations (`tool_use_failed`, reasoning tokens inside the
ceiling) that a generic OpenAI-compatible client would get wrong in ways that produce
plausible-looking wrong answers rather than errors. And the standing cost of Decision 4: a provider's
error body is untrusted input, so every future vendor module owes the same allowlist discipline rather
than the obvious `error.message`.

**Verification.** The suite is **1255 tests**, up from Phase T's 1209, all passing. `alembic check`
reports *"No new upgrade operations detected"* — the mechanical proof that this adds no table, no
column, and no index, which is what a change confined to the provider layer should look like. `ruff
check`, `ruff format --check`, and `mypy app alembic` are clean across 113 source files.

Four breaks were made deliberately, and each was confirmed to fail the test that covers it, because a
test that cannot fail is not testing anything. Deleting the token re-attachment in `groq.py` failed
`test_arguments_that_break_the_schema_are_refused_with_their_tokens` and nothing else. Mapping
`tool_use_failed` by status instead of by code failed two. Removing the pairing validator from
`Settings` failed two in `tests/unit/test_config.py`. Replacing the `error.code` allowlist with the raw
`error` object failed five, including
`test_a_groq_failed_generation_neither_reaches_the_log_nor_the_reason` — the one that matters, since it
is the test that would catch a customer's own words arriving in a log line through a provider's error
body. Each break was reverted and `groq.py` and `config.py` confirmed byte-identical to their backups.

**The live end-to-end ran, and Phase T's outstanding claim is discharged.** `uvicorn` under
`app.core.event_loop:loop_factory`, then `scripts/phase_t_walkthrough.py` against
`openai/gpt-oss-120b`: four real calls answered, one deliberate permanent failure, and the dashboard
read back over HTTP in another process. The script's own log, trimmed:

    provider=groq model=openai/gpt-oss-120b
    4 calls in 3.0s
    classify said: 'Billing' / 'Duplicate Charge'
    the call failed as intended: AIServiceError

    operation         ok        in    out        cost      ms  provider/model
    classify          no         0      0    0.000000     142  groq/openai/gpt-oss-120b
    classify          yes      463     76    0.000115     692  groq/openai/gpt-oss-120b
    sentiment         yes      347     54    0.000084     540  groq/openai/gpt-oss-120b
    summarize         yes      463     74    0.000114     655  groq/openai/gpt-oss-120b
    suggest_response  yes      591    138    0.000171     961  groq/openai/gpt-oss-120b

    {'calls': 5, 'failed_calls': 1, 'prompt_tokens': 1864, 'completion_tokens': 357,
     'cost_usd': '0.000493', ...}

    **34 passed, 0 failed**

Three things in that output are the point rather than the decoration. The **failed row carries zero
tokens and zero cost** while still being a row — a 401 is refused before anything is generated, so the
ledger records an attempt that cost nothing rather than omitting it, which is what makes
`failed_calls` a count of attempts instead of a count of mistakes. The **latency column is real**
(142–961 ms), which no test double can show. And the **second organization reads zeros** on every
field while the first reads 0.000493, which is ADR-026's aggregate and §4's tenant isolation checked
in the same request.

**One pre-existing defect was found by running it, and it is not in this change.** The suite had been
reading the developer's `.env` for two of its own premises: four tests assert an exact `cost_usd` whose
figures are only true for `claude-sonnet-5`, and one of them pinned the provider's view of the model
while the ledger priced from the ambient one. They were correct for exactly as long as every machine's
`.env` said `claude-sonnet-5`, and failed the first time one said `openai/gpt-oss-120b` — a suite
reporting on the reader's environment rather than on the code. `tests/conftest.py` now pins
`AI_PROVIDER`/`AI_MODEL` to the committed defaults, as it already pins every other setting, and says
why in the one block there that assigns rather than `setdefault`s. The model was always part of those
tests' premise; now the tests state it.

## ADR-029 — A worker has a tenant and no context, and the model names the band, not the priority

**Status:** accepted · Phase U

**Context.** §18 writes the analysis out as nine steps, and until this phase every one of them was
aspirational. Phase T built the call — one provider boundary, one call path, retry, validation, and a
ledger that records every attempt — and deliberately shipped **no route, no task, and no
`ai_analyses` row**: `tests/integration/test_ai_usage_ledger.py`'s first paragraph says so, and
ADR-027's own *"What this deliberately does not do"* names §36's three AI endpoints as Phases U, V,
and W. Phase T also left the prompt text unwritten, recording it as *"mechanism in T; text in U-W"* —
`app/ai/prompts.py` held `defuse` and `as_untrusted` and not one sentence of instruction.

Three pieces of scaffolding had been naming this phase for several phases, each with a comment saying
so: `websocket/events.py` had `AI_ANALYSIS_COMPLETED` in `UNPUBLISHED_EVENT_TYPES` *"so that Phase T-W
has an entry to delete"*; `notification_service.py` had it in `DEFERRED_EVENT_TYPES`, whose comment
named *"Phases T-W, which build the analysis this would announce"*; and `celery_app.py` did not declare
the `ai` queue, on the rule that *"declaring a queue nothing publishes to is a worker process waiting
for work that does not exist."* This phase is the producer all three were waiting for.

Everything the analysis *writes to* already existed. `AIAnalysis` has its lifecycle, its indexes, and
the `terminal_status_has_payload` constraint; `tickets` has `category`, `subcategory`, `sentiment`,
both confidences, `ai_recommended_priority`, and `ai_priority_score`, all nullable with the comment
*"analysis is asynchronous, so a ticket is fully usable before any of these are populated."*
**So this phase adds no migration** — `alembic check` staying green is the mechanical proof.

Two decisions had been taken with the user before any of this was written, and both are decisions
rather than defaults: **the model names the priority band** (the classification call returns category,
subcategory, priority, and confidence together, rather than a fifth provider operation), and **the
recommendation is advisory** (the worker never writes `tickets.priority`).

**Decision 1 — `WorkerContext` is a type that carries a tenant and no authority.** `ai_service`'s four
functions took a `TenantContext`, and `_stage_usage` said why: *"nothing in this function's signature
could carry one even if a caller wanted to pass one, which is the point of taking a context rather than
an id."* A Celery task has no request — and `sla_repository.py` refused to fabricate one in Phase Q, in
terms that apply unchanged here: *"`role` would have no honest value at all: it decides `permissions`,
and there is no role whose permissions describe 'the scheduler'."*

`app/core/tenancy.py` therefore gains `WorkerContext`: a frozen dataclass holding one
`organization_id`. It has **no `role`, no `permissions`, no `has()`, and no `scope_for()`** — it cannot
answer an authorization question because it cannot be asked one, so a task cannot grant itself a
capability by holding a value. `TenantScopedRepository` still takes a `TenantContext` and does not
accept this, which is what keeps the worker out of the request path's row-scoped queries entirely and
keeps `sla_repository`'s property true: the context-free queries stay small enough to count, in one
file each.

**It has exactly one consumer: `app/services/ai_service.py`**, which takes it so the ledger row can
name the tenant that paid. Passing a bare `uuid.UUID` would compile and would work and would give up
the property that makes a wrong tenant unpassable — a `WorkerContext` is a type that says *this tenant,
no caller*, and an id says nothing at all.

**Decision 2 — the model names the band, on the call it was already making.** §51 asks for
classification, category, subcategory, sentiment, *priority recommendation*, and confidence.
`Classification` gains one field, `priority: TicketPriority`, so the recommendation arrives with the
classification rather than in an operation of its own. §17's provider interface has exactly four
operations and a fifth would need a new `AIOperation` member — a database migration for a value the
model can answer in a call it is already making.

The rejected alternative is worth stating because it is the obvious one: a Python rule that bands a
category and a sentiment. It was refused because `tickets.ai_recommended_priority`'s own column
comment reads *"what the model suggested"*, and a rule's output is not a suggestion. The prompt is
also where the four `TicketPriority` values are explained by name, which is the right home for them:
the vocabulary is ours, so it is ours to define, and the model is being taught it rather than asked to
guess it.

**Decision 3 — the recommendation never becomes the priority.** The worker writes `tickets.category`,
`subcategory`, `ai_recommended_priority`, and `ai_classification_confidence`; it writes
`tickets.sentiment` and `sentiment_confidence`; and it **never** writes `tickets.priority`.
`ticket_service.change_priority` remains its only writer, and `POST /tickets/{id}/priority` is how the
suggestion is applied. §6 keeps the model's answer and the business decision in two columns so they can
be compared, and that comparison is vacuous if the worker fills in both — a system that applied its own
recommendation could never be measured against it, and §60's prohibition on AI modifying critical
business data without validation would be met only formally.

`tickets.ai_priority_score` is left **NULL with no writer**, and this is deliberate rather than
unfinished: the band *is* the recommendation, and a second number expressing the same judgement with
nothing reading it is the kind of column this codebase refuses elsewhere.

**Decision 4 — one task runs both operations, because §18's last three steps are singular.** The ticket
gets one timeline entry, one notification, and one realtime announcement — not one per operation. Two
tasks would each independently decide to publish, so a client would re-read the same ticket twice and
an agent would get two alerts for one arrival. Inside the task, however, **a failure is contained to
its own operation**: each call is caught separately, its row is marked `failed`, and the rest of the
run continues, which is §7's *"AI provider failure degrades gracefully"* made concrete. Nothing is
retried here — retry is `ai_service._run`'s policy, and a second loop would multiply the two.

**Decision 5 — the rows are written before the work is queued.** `request_analysis` creates one
`pending` row per operation, stamps the configured `provider` and `model` on each, writes the audit
entry, commits, and *then* hands the task to the broker. `AIAnalysis`'s own docstring gives the
reason — *"a pending or failed analysis is visible rather than silently absent"* — and writing first is
what makes it true: a client that asks and reads back immediately sees two `pending` rows rather than
an empty list. The stamp is taken at queue time and never re-read from config, so a historical analysis
still names the model that was asked even after a deployment changes it.

The order is not allowed to invert: the task's first act is to read the rows it was handed the ids of,
so a task that started before the commit would find nothing and quietly do nothing. And a **broker that
is down does not fail the request** — the rows are committed and show as `pending`, the user's action
succeeded, and only the work is late; the exception is logged by type, without a traceback, because a
connection error's message embeds a URL that carries a password in production (§4).

The rows are also **not** marked `failed` when the queue is unreachable, which is the opposite of what
a provider failure does. A failed call is an answer about the model; a broker that is down is an answer
about the infrastructure, and marking the row `failed` would claim the model said nothing when it was
never asked — and would discourage the retry that is the correct response.

**Decision 6 — the read route needs the AI capability, not `TICKET_VIEW`.** §36 names
`POST /tickets/{id}/analyze` and two routes that belong to Phases V and W. This phase adds that one
plus a second that §36 does not name — `GET /tickets/{id}/ai/analyses`, the latest row per operation —
because `pending` and `failed` have to be observable, which is the whole reason a row exists from queue
time. **Both are guarded by `AI_REQUEST_ANALYSIS` and neither by `TICKET_VIEW`**, and that is the
decision rather than an implementation detail: `TICKET_VIEW` is held by every portal account, so
guarding the pair with it — the obvious choice, since a ticket is what they hang off — would hand a
customer the analysis of their own ticket, including `error_message`, whose column comment keeps it
from customers because *"upstream errors can echo prompt content."* §3 gives the portal no AI access at
all, so this is the whole capability and not a redaction.

A cross-tenant read is a **404 and not a 403**, and the refusal is byte-identical to a request naming a
uuid that never existed — asserted in full, body and headers, on both verbs. There is no
`GET /ai/analyses/{id}` for the same reason: an analysis is addressed through the ticket that owns it,
so there is no id to guess and none to refuse (ADR-015).

**Decision 7 — the AI limit is keyed by user, and only the verb that spends money is limited.** §45
names *"AI endpoints"* and there were none. `limit_upload`'s reading applies unchanged: this runs
*after* authentication, the abuse is one account's, and an AI call costs money in a way a login attempt
does not — so the key is a user id rather than an address, and an anonymous request is refused by the
auth layer before the limiter counts anything. A fresh setting, `RATE_LIMIT_AI_PER_HOUR`, and
`app/api/rate_limits.py` gains a row so that *"read the guards here and you have read every limit the
API applies"* stays true.

The read route is **not** limited. A guard on it would cap how often a client may look at work it has
already paid for — a limit that makes a UI feel broken without protecting anything.

**Decision 8 — the worker writes its own timeline entries, and the notification sets go from four to
three.** §18's step 7 is a timeline entry, and the natural call is `ticket_service.record_event`. That
function takes a `TenantContext` because it names an actor, and a worker has none. `sla_tasks._record`
answered this in Phase Q by building the event itself, and its comment already anticipated this case by
name: *"NULL when the system acted rather than a person — SLA breaches and completed AI analyses have
no actor."* The analysis follows that precedent rather than widening `record_event`, so `WorkerContext`
acquires no second consumer and `actor_user_id` is `NULL` — which is a fact about the row, not a
missing value.

The same reasoning settles the realtime event and the notification. `AI_ANALYSIS_COMPLETED` moves out
of `UNPUBLISHED_EVENT_TYPES` into `REALTIME_FOR_EVENT`; **`DEFERRED_EVENT_TYPES` becomes empty and is
deleted** rather than left as a set with no members awaiting a consumer; and `SCHEDULED_EVENT_TYPES`
becomes **`WORKER_EVENT_TYPES`**, because its own docstring already said the division was *"which code
path sends it, not whether it is sent"* — and with the analysis in it, the property is *produced by a
worker*: on a clock for the SLA pair, on a queue for this one.

The recipient is `ticket.assigned_agent_id`, and **nobody** when it is `NULL`. That is the reading
`test_a_customer_reply_on_an_unassigned_ticket_notifies_nobody` already asserts — *"Nobody is working
it, so there is no one person to alert"* — and an analysis of an unassigned ticket is visible on the
ticket itself; inventing a fan-out would make it indistinguishable from the notification centre.

**Decision 9 — the ledger's tenant and the analysis row's tenant come from the same value, and the
worker's `user_id` is `NULL`.** `ai_service._stage_usage` reads `context.organization_id` for the row
and `context.user_id` for the actor; a `WorkerContext` has no user, so the ledger row's `user_id` is
`NULL` — which is what `AIUsage.user_id`'s column comment already anticipated: *"a background embedding
job has neither."* The query that isolates one tenant's spend from another's is therefore the same
column request-path calls write, and a mismatched tenant on a task is a **cross-tenant write no API
test could reach**, since no route calls these queries. `tests/security/test_ai_isolation.py` drives
the task directly with a deliberately wrong tenant in each direction.

**Decision 10 — the worker reaches Redis through a scoped client.** `event_loop.run` builds **and
closes** a loop per task invocation, so a client cached on the shared module would be bound to a loop
that no longer exists by the next task — the trap `sla_tasks` documents. The worker uses
`redis.scoped_client()`, and `cache.invalidate` gains an optional `client` parameter, exactly as
`realtime.publish` already had one and for the same reason. Both publishes and the invalidation happen
**after the commit** and never raise, so a Redis outage cannot turn a completed analysis into a task
Celery retries — the rows are the durable record and they are already written. The invalidation is here
and not in `ai_service` because the ticket's AI fields move every cached analytics figure computed over
them, and ADR-027 records why `ai_service` cannot do it: invalidating before the caller's commit lands
is a race, so whoever commits owns the invalidation.

**Decision 11 — a provider client belongs to a loop, not to the process.** Decision 10 is right, and
this phase's live end-to-end proved it was not applied everywhere it needed to be. The first
`analyze_ticket` a worker received succeeded in 2.1s; the second, fifteen seconds later, raised
`RuntimeError('Event loop is closed')` from inside `httpx`'s transport — after the request had been
built and before it was sent. `app/ai/groq.py` and `app/ai/claude.py` both cached their client in a
module global, and `event_loop.run` builds **and closes** a loop per task, so the client the first task
built pointed at a loop the second task could not use. The failure is worth stating precisely because of
its shape: it is invisible to the entire suite (where one loop serves every test) and it appears only on
the *second* task, so a single-call smoke test passes and production fails on every ticket after the
first. Decision 10's own reasoning, applied to the other two caches in the worker's path.

The fix is a cache keyed on the running loop, asked through a new `event_loop.current_loop()`, which
returns `None` outside one. A stale client is **dropped, never `aclose()`d** — closing it would need the
loop it was built on, which is the thing that is gone — so the fix trades a small resource leak for
correctness on the path that runs. The three alternatives were each rejected: building a client **per
call** repeats a TLS handshake inside a call whose latency is dominated by the model thinking, which is
what the cache exists to avoid; **resetting it from `ai_tasks`** would leak provider internals into the
worker, which is the boundary ADR-028 is about, and there will be a third vendor in Phase X;
**holding the loop weakly and rebuilding on collection** is the same check with a slower way of asking.
`current_loop()` lives in `app/core/event_loop.py` rather than in either vendor, because that is the
module that *creates* the loops and its docstring is already where the build-and-close lifecycle is
explained — a vendor module importing it is a vendor module asking the loop owner a question about the
loop, which is the right direction.

**What this deliberately does not do.** No summarization, no suggested replies, no RAG — §20, §21, and
§22 are Phases V, W, and X, and `AIOperation.SUMMARIZE`, `SUGGEST_RESPONSE`, `EMBED`, and
`KNOWLEDGE_ANSWER` stay declared-and-unused as the other phases left them. **No retry sweep for stuck
analyses**: `ix_ai_analyses_pending` is partial on exactly `pending` and `processing`, so the index is
already built for one, but nothing reclaims a row whose worker died mid-run. That is recorded here as
the next phase's problem rather than hidden — the row is visible and the index that finds it exists.
**No `ai_priority_score`**, per Decision 3. **No embeddings** — `EMBEDDING_MODEL` stays
declared-and-unused for Phase X per ADR-008, and `generate_embedding` is still absent from the protocol.
**No report tasks**; `report_tasks.py` remains unbuilt and is still nobody's business this phase.

**Cost.** No new dependency, no migration, and no new setting beyond `RATE_LIMIT_AI_PER_HOUR`. Two new
modules (`ai_repository.py`, `ai_analysis_service.py`), one task module, one route module, and one
schema — plus the standing cost of `WorkerContext`: every future background task owes the same
discipline, and `ai_service` is now the one function in the codebase whose signature accepts two
context types, which is a union a reader has to hold. The union is the price of not fabricating an
identity, and it is paid once, at the boundary where the ledger is written.

**Verification.** The suite is **1322 tests**, all passing — 1255 at ADR-028's close, 65 for this
phase's own surface, and 2 for the defect the live run below turned up. `alembic check` reports *"No new
upgrade operations detected"*, which is the mechanical proof of this phase's central claim: it writes six
`tickets` columns, an `ai_analyses` lifecycle, a ledger row, and a timeline entry, and every one of them
already existed. `ruff check`, `ruff format --check` (187 files), and `mypy app alembic` are clean across
118 source files.

**Four breaks were made deliberately, and each was confirmed to fail the test that covers it** — the
alternative being a test suite that would have passed before the phase started. Swapping the terminal-status
filter in `run_analysis` for `list(analyses)` failed `test_a_redelivered_task_does_nothing` and nothing else,
with the model being asked a third time on a two-outcome script: *"FakeProvider was called 3 times but only
2 outcomes were scripted."* Adding `ticket.priority = classification.priority` beside the recommendation
failed `test_the_recommendation_never_becomes_the_priority` and nothing else. Moving the read route from
`AI_REQUEST_ANALYSIS` to `TICKET_VIEW` failed two — the API test that a portal customer is refused, and
`test_every_route_declares_the_capability_the_matrix_assigns`, which is the one that makes the capability
matrix a checked statement rather than a document. And deleting the loop check Decision 11 describes failed
`test_the_shared_client_is_rebuilt_when_it_is_on_a_loop_that_has_gone` alone in each vendor's file —
`assert <httpx.AsyncClient object at 0x...> is not <httpx.AsyncClient object at 0x...>`, the same object
compared with itself, which is precisely the bug. Each was reverted and all five touched files confirmed
byte-identical by SHA-256 against hashes taken before the first break.

**The live end-to-end ran against a real key, and it found the bug Decision 11 records.** `uvicorn` under
`app.core.event_loop:loop_factory`, a Celery worker on `--pool=solo -Q notifications,sla,ai`, and
`scripts/phase_u_walkthrough.py` raising a real ticket, against the real `openai/gpt-oss-120b`:

    provider=groq model=openai/gpt-oss-120b
    1. §18 steps 1-2: the ticket is raised, and the analysis is a message
      ok    the ticket was created
      ok    nothing has been concluded yet
      queued: ['classify', 'sentiment']
      ok    two rows exist, one per operation
      ok    they name the provider and model that will be asked
    2. §18 steps 3-7: the worker classifies it, in another process
      settled after 2.1s: {'classify': 'completed', 'sentiment': 'completed'}
      category: 'Billing'   subcategory: 'Duplicate Charge'   sentiment: 'negative'
      sentiment_confidence: 0.99   ai_recommended_priority: 'high'
    3. §6: the model's band lands beside the priority, never on it
      ok    the priority is the one the customer's ticket was created with
      priority='low' vs recommended='high' -- the model recommended something else
    5. §28: the ledger grew, read in a third process
      {'calls': 2, 'failed_calls': 0, 'prompt_tokens': 1660, 'completion_tokens': 124,
       'cost_usd': '0.000323', ...}
    7. §6: a second analysis adds rows rather than overwriting them
      ok    the button answers 202
      ok    the rows served now are new ones
      ok    and they finished too
    34 passed, 0 failed

The first run of that script **failed**, and the failure is the most useful thing this phase produced. The
worker's first `analyze_ticket` succeeded in 2.1s; the second, fifteen seconds later, was received and died
152ms after — `RuntimeError('Event loop is closed')`, raised inside `httpx`'s transport after the request
was built and before it was sent, because both vendor modules cached their client on the process while
`event_loop.run` closes each task's loop. **No test in the suite could have found it**: one loop serves
every test, so the cache is never stale there, and the failure needs a *second* task, so a single-call
smoke test passes and every ticket after the first fails in production. After the fix, the same script
against the same key produced three tasks and three successes in the worker's own log — the third being the
regenerated analysis of the same ticket, which is exactly the case that had died:

    11:57:24 ai_analysis_complete completed=2 failed=0 ... ticket_id=6366dafe-0966-4bb4-b401-f736023e7a2d
    11:57:41 ai_analysis_complete completed=2 failed=0 ... ticket_id=6366dafe-0966-4bb4-b401-f736023e7a2d

Two things the script states it cannot show for itself, and they are recorded rather than papered over: the
timeline entry (written with a `NULL` actor by a code path that has no route, so it is read in `psql`), and
§6's separation *deterministically* — section 3 asserts `priority` was untouched, but a run where the model
happened to recommend the band the ticket already had cannot tell the two columns apart, which is why the
deliberate break above is what pins it.

---

## ADR-030 — A summary is stale when a person speaks, and a cache hit is still a ledger row

**Status:** accepted · Phase V

**Context.** §20 is four sentences — *"For long ticket conversations, generate a concise summary…
Store the latest summary. Allow regeneration when the conversation changes significantly. Avoid
regenerating the summary after every tiny message if unnecessary."* — and §36 names the route that
serves them, `POST /api/v1/tickets/{id}/ai/summarize`, between the analysis route Phase U built and
the suggestion route Phase W will.

Everything below the flow already existed, which is the reason this phase is small and the reason its
central claim is mechanical rather than argued: `AIOperation.SUMMARIZE` (`app/models/enums.py:170`),
`ConversationSummary` (`app/schemas/ai.py:102`), `AIProvider.summarize_conversation` in the protocol
and all three implementations, and `ai_service.summarize_conversation` with its ledger row. The
`ai_analyses` table is operation-agnostic and `latest_by_operation` already serves whatever
operations a ticket has. **So this phase adds no migration** — `alembic check` staying green is the
proof, and it is the second phase in a row to make that claim.

Three pieces of scaffolding named this phase and are discharged rather than left standing.
`app/services/ai_service.py` said *"`was_cached` is always `False`. Phase T has no AI result cache,
and §20's 'avoid regenerating' belongs to Phase V, which is where a cached call will be able to say
so"* — that paragraph is now written in the past tense, because the claim is true rather than
promised. `app/services/ai_analysis_service.py`'s `ANALYSIS_OPERATIONS` comment listed summarization
as a later phase's; it stays a **two-member tuple**, and the comment now says why. And
`app/repositories/ai_repository.py` predicted that `load_analyses` *"will be more when Phase V adds
summarization"*. **It does not**, and the prediction is corrected rather than left standing: a wrong
prediction in a docstring is a wrong claim.

Three decisions were taken with the user before anything was written, and all three are decisions
rather than defaults: the cache is a **freshness check** and not a result cache; **any new message**
makes a summary stale, with no threshold and no `?force=`; and the model reads **people, notes
included**, excluding system entries and unsent drafts.

**Decision 1 — the cache is a comparison between two stored timestamps, not a cache.** When a
summary is asked for and no eligible message has arrived since the last one was made, the stored
summary *is* the answer: `request_summary` returns it, creates no `ai_analyses` row, queues nothing,
and records the call it did not make. There is no Redis key, no TTL, and **no change to
`ai_service._run`** — nothing is added inside the retry loop, where a cache would have to reason
about attempts rather than about answers.

The rejected alternative is the obvious one — a result cache in `app/core/cache.py` keyed on the
conversation — and it was refused for two reasons. It would be machinery serving exactly one caller:
a summary is **the only AI operation whose input can be identical twice**, because every ticket's
classification text is new text by construction, so a generic cache would have one tenant and a
lifetime of complexity. And §20's first sentence already asks for the thing the cache would need —
*"Store the latest summary"* — so a second store of one fact is a second thing to keep consistent,
which is how the freshness check and the model would come to disagree about what changed.

**Decision 2 — any new message makes a summary stale, and nothing runs on a message.** §20 says
*"changes significantly"* and *"every tiny message"*, both of which invite a threshold. The decision
is that there is none: one new eligible message is a change. What actually prevents regenerating on
every tiny message is not a rule about messages but the absence of one — **no message insert queues
anything**, so a summary is only ever made because a person asked, and a person who asks twice wants
the current answer rather than a refusal. A `?force=` parameter was considered and rejected as the
worse version of the same thing: an explicit request is already the strongest statement of intent
available, and honouring it only when a flag is present makes the ordinary case wrong.

**Decision 3 — the model reads what people said, internal notes included, and the exclusion is one
predicate with two readers.** Customer and agent messages are eligible; `system` entries and
`ai_draft` rows are not. A system row is a status change rather than anything anybody said, and a
draft is unsent — §21's *"AI must NEVER automatically send a customer-facing response"* made a data
question, because a summary of what people said should not contain a draft nobody sent.

Internal notes **are** included, and that is the decision worth stating. §20's summary is shown to
staff beside the ticket, the route is gated by `AI_REQUEST_ANALYSIS` and never by `TICKET_VIEW`, and
the note is often where an agent writes down what was actually promised — so a summary that omitted
it would be a worse summary for everybody who can read it, and nobody who can read the summary is
anybody who could not already read the note.

`_CONVERSATION_SENDERS` in `app/repositories/ai_repository.py` is that rule written once, with two
readers: `conversation_watermark` and `load_conversation` compose the same fragment, so the freshness
check and the prompt cannot come to describe different conversations. The failure that prevents is
the bad direction — a summary served as current that never saw the message which made it stale —
and it is the reason `MESSAGE_FTS_EXPRESSION` is a shared constant rather than a comment asking two
sites not to diverge.

**Decision 4 — the watermark compares `created_at` to `created_at`, and the direction of its error is
conservative.** Both `messages.created_at` and `ai_analyses.created_at` are written by the database
(`server_default=func.now()`), so no application-versus-database clock skew enters the comparison.
The summary row is written *before* the task that fills it runs, so a message arriving in that window
makes a summary that did in fact cover it look stale. That costs one regeneration and never serves a
summary that missed a message. `completed_at` would have been the app clock compared against a
database one, and this is the column whose error runs in the safe direction.

**Decision 5 — a cache hit is a ledger row, and `was_cached` finally has a writer.** The call that
did not happen is recorded, with zero tokens, zero cost, zero latency, and `was_cached` set. It would
have been simpler to write nothing, and that is exactly the wrong choice: `ai_usage`'s own column
comment says the flag exists so *"the cache's actual saving be measured instead of estimated"*, and a
saving measured by the absence of a row is an inference, not a count. With the row, the saving sits
*because* of the calls it saved — `calls` and `cached_calls` in the same aggregate, one a subset of
the other for the reason `failed_calls` is.

`ai_service.record_cache_hit` is the only writer, and it records **the provider and model of the
stored summary, not the current configuration** — the row names the model whose answer is being
reused, which is the fact a ledger row is supposed to carry. `_stage_usage` gains `was_cached` as a
keyword with a default of `False`, so the three call sites inside `_run` are unchanged: a call that
reached the provider is a call that was not cached, which is the whole claim the old docstring was
protecting. And `AIUsageSummary` gains `cached_calls` in the same commit, because a column written
and never read is the column-with-no-reader this codebase refuses elsewhere.

**Decision 6 — summarization is a second entry point over the same worker, not a third operation.**
`request_summary` writes one `SUMMARIZE` row where `request_analysis` writes two, and everything
below the entry point is shared: the same `run_analysis`, the same task, the same terminal-status
skip, the same failure containment, the same ledger. A second worker path would be a second place
for §16's duplicate guard to be missing from — and §20's summary is not something "analyze this
ticket" performs, since what it reads is the conversation rather than the ticket's two text fields.

**Decision 7 — `_reduced` stops assuming every answer has a confidence.** `ConversationSummary` has
one field, so `_Outcome.confidence` becomes `float | None`, the summary branch returns `None`, and
`ai_analyses.confidence` stores a `NULL` — which its nullable column and range check already allow.
The alternatives were both worse: a `0.0` would be a claim the model never made about a judgement it
was never asked for, and `getattr(value, "confidence", None)` would let §18's and §19's schemas lose
their confidence silently. The reduction reads the field only from the two schemas that have one, by
name, so a renamed field is a type error rather than a `None` in a column.

**Decision 8 — a run that changed no ticket column announces without alerting.**
`notification_service.notify_analysis_completed` still exists and still fires for an analysis run;
it does not fire for a summary-only run. The alert exists because a classification changes the
ticket and somebody should look; a summary changes nothing and exists to be read by whoever asked
for it, so an alert would interrupt the requester about a page they are already on. The timeline
entry and the socket event **do** still happen — the entry is the only record of *when*, and the
event is what a client polling between asking and the answer arriving is waiting for — so the run is
not silent, it is only quiet.

**Decision 9 — an empty conversation is refused, and the refusal is a `ValidationError`.**
`ticket_service.create_ticket` writes the description onto the ticket and creates **no** `Message`
row, so a freshly raised ticket has a conversation of length zero. §20 is about *"long ticket
conversations"*, and summarizing nothing would hand the model an empty block and spend a call
learning that. `request_summary` therefore checks the watermark's count and raises
`ValidationError("This ticket has no conversation to summarize.")` — §42's existing `422`, the same
code and the same class `create_ticket` uses for a rule Pydantic cannot express, rather than a new
error code for a case that is not a new kind of failure.

**Decision 10 — a conversation longer than the model's context fails rather than being truncated.**
`load_conversation` has no `LIMIT` and no character budget. An over-long conversation is refused by
the provider, the row is marked `failed`, and `error_message` says so on the ticket — a visible
failure. Truncating to fit would produce a summary that silently omitted the beginning and was
indistinguishable from one that had read it all, which is a wrong answer wearing the same shape as a
right one, and §60's ban on fake AI results is about exactly that. A budget that keeps the newest
turns *and says what it dropped* is the honest version of the limit, and it is not in this phase.
This is recorded as a limitation rather than a plan.

**What this deliberately does not do.** No suggested replies (§21, Phase W) and no knowledge base,
embeddings, or retrieval (§22, Phase X) — `SuggestedReply`, `AIOperation.SUGGEST_RESPONSE`,
`EMBED`, and `KNOWLEDGE_ANSWER` stay declared-and-unused exactly as they were. **No automatic
summarization**: nothing is summarized because a message arrived, per Decision 2. **No
message-count threshold and no `?force=`**, per Decision 2. **No summary column on `tickets`**: the
summary is the newest `SUMMARIZE` row, which `GET /tickets/{id}/ai/analyses` already serves and
`latest_by_operation` already reduces to one per operation — §20's *"store the latest summary"* is a
reading, not a second copy. **No retry sweep for stuck analyses**, which remains ADR-029's recorded
gap and is still covered by `ix_ai_analyses_pending`.

**Cost.** No new dependency, no migration, and no new setting. One new public function
(`ai_service.record_cache_hit`), one new repository function group, one route, one field on
`AIUsageSummary`, and one more column on a query that already ran. The standing cost is the
asymmetry `ANALYSIS_OPERATIONS` now documents: "what one analyze request does" and "what the AI
worker can be asked to do" are no longer the same set, so a reader has to hold both — which is the
price of §20 reading the conversation while §18 and §19 read the ticket.

**Verification.** The suite is **1357 tests**, all passing — 1322 at ADR-029's close, so 35 for this
phase's own surface. `alembic check` reports *"No new upgrade operations detected"*, the mechanical
proof of the claim above: this phase writes an `ai_analyses` lifecycle and a ledger row and every
column already existed. `ruff check`, `ruff format --check` (191 files), and `mypy app alembic`
(118 files) are clean.

**Four breaks were made deliberately, and each failed the tests that cover it.** They come in two
pairs, and the pairing is the interesting part.

*The freshness rule.* Deleting the comparison in `request_summary` — `if stored is not None` — so
that any stored summary is always the answer, failed **`test_one_new_message_makes_the_summary_stale_again`
and nothing else**. That is worth recording: the two cache-hit tests *pass* under this break, because
a route that never regenerates satisfies "a second ask returns the stored row" perfectly. Making
`conversation_watermark` report `now` instead of the newest message's timestamp — so that every
summary always looks stale — failed **`test_a_second_summary_with_nothing_new_returns_the_stored_one_for_free`
and `test_a_current_summary_comes_back_completed_and_queues_nothing`**, and leaves the staleness test
green. Neither break alone covers the rule; the comparison has two directions and each test file
pins one of them. A single break that "made the cache tests fail" would have proved nothing about the
other direction, which is why both were made.

*The ledger.* Defaulting `was_cached` to `True` on every staged row — marking real provider calls as
free — failed **`test_a_cache_hit_is_a_row_and_the_overview_counts_it`**,
**`test_a_committed_call_leaves_a_row_in_the_table`**,
**`test_the_overview_route_reports_the_ledger_it_used_to_read_empty`**, and the ledger assertion
inside `test_a_second_summary_with_nothing_new_returns_the_stored_one_for_free`. Four tests across
two files, which is the claim that `was_cached` is checked where a call is real and not only where it
is skipped.

*The eligibility rule.* Widening `_CONVERSATION_SENDERS` to include `SenderType.SYSTEM` failed
**`test_the_conversation_is_customer_and_agent_messages`** (the constant itself),
**`test_every_sender_type_is_eligible_or_deliberately_excluded`** (the mechanical sweep over the
enum), and **`test_the_model_reads_the_people_and_not_the_books`** (the prompt a scripted provider
actually received). The third is the one that matters: the first two are assertions about a constant,
and only the third is about what the model was handed.

Each break was reverted, and the reverts confirmed by re-running the affected files green.

**The live end-to-end ran against a real key.** `uvicorn` under
`app.core.event_loop:loop_factory`, a Celery worker on `--pool=solo -Q notifications,sla,ai`, and
`scripts/phase_v_walkthrough.py` raising a real ticket, posting a customer message and an agent's
internal note over HTTP, and summarizing against the real configured model:

    provider=groq model=openai/gpt-oss-120b

    1. §20 sentences 1-2: a conversation exists and nothing is summarized yet
      ok    the ticket was created
      customer: Ada Lovelace  ticket: 1
      the ticket's own §18 analysis settled: completed
      ok    a customer message and an internal note were posted
      ok    no summary exists yet
      queued: summarize  status=pending
      ok    the route accepted the request
      ok    it answered with one row, queued or already done
      ok    the row names the model that will be asked
    2. §20 sentence 1: the worker summarizes it, in another process
      settled after 2.1s: status=completed
      summary: 'The customer reports that attempting to download a file in Safari still
      results in an error and the download never starts. The agent has internally
      identified that the file store is degraded, returning 503 errors to all users, and
      has opened platform ticket PLAT-4417. No timeline has been promised pending
      resolution of that ticket.'
      ok    it is a real sentence somebody could read
      ok    the row carries no confidence
      ok    the call was billed for its tokens
      ok    the summary changed nothing on the ticket
      priority='medium'  recommended='medium'
    3. §20 sentence 4: asking again with nothing new costs nothing
      before: calls=3 cached=0 cost=0.000512
      ok    the same row came back
      ok    and it came back completed, not queued again
      after:  calls=4 cached=1 cost=0.000512
      ok    the cached call is counted
      ok    the call count moved with it -- cached_calls is a subset, not a deduction
      ok    and the bill did not move at all
    4. §20 sentence 3: one new message makes the summary stale
      ok    a new customer message was posted
      queued: summarize  status=pending
      ok    a new row was queued rather than the old one served
      ok    the regenerated summary completed
      summary: 'The customer reports that downloads fail with a 503 Service Unavailable
      error, even after trying Safari again. An internal note indicates the file store is
      degraded and returning 503s to all users, with platform ticket PLAT-4417 opened, and
      no timeline promised. The issue remains unresolved.'
      ok    it is a different summary
    5. §53: the same ticket id, asked by another tenant
      ok    the stranger's request is a 404
      ok    and it names no ticket
      ok    the owner still reads their own summary

    24 passed, 0 failed

Section 3 is the phase's whole claim in three lines. A real model summarized a real conversation,
the second ask was answered from storage, and the numbers say so without any interpretation: the
call count went up by one because a ledger row was written, `cached_calls` went up by one because
that row is marked, and **`cost_usd` is identical to the cent** because no provider was asked. §20's
fourth sentence is not a policy here, it is `0.000512 == 0.000512`. Section 2's summary names
PLAT-4417 — the platform ticket that exists only in the agent's internal note — which is Decision 5's
reading decision visible in the model's own output.

**The notification absence was then checked in the database rather than left as an argument:**

    notifications_for_that_ticket
    ------------------------------
                                0
          event_type       | count
    -----------------------+-------
     created               |     1
     message_added         |     2
     internal_note_added   |     1
     ai_analysis_completed |     3

Three `ai_analysis_completed` timeline entries — the ticket's own §18 run and the two summaries — and
zero notifications. **That zero is weaker than it looks, and the reason is worth stating.** This
ticket was never assigned, and `notify_analysis_completed` sends to the assignee and to managers, so
the classification run would have produced nothing either. The count alone cannot separate "summaries
do not alert" from "nobody was listening" — which is exactly why the assertion that carries Decision 8
is `test_a_summary_only_run_announces_without_alerting`, a test that assigns an agent, runs the
summary, and asserts the notification list is empty anyway. The query above is a sanity check on the
story, not the proof of it.

One thing was found by running this rather than by writing it: the first run's output arrived as
mojibake. Windows redirects stdout to the locale encoding, cp1252, which has no `§`, so the section
headings came back as replacement characters — and a decision record whose transcript is unreadable
is not evidence. The script now asks for UTF-8 explicitly, and the block above is a capture of the
second run.


The script's own closing note records three things it cannot show for itself, and the honest one is
the first. **The notification that is *not* sent** has no HTTP surface at all — a run that changed no
ticket column announces on the socket and writes its timeline entry, and then nothing arrives, which
on every other surface looks exactly like a message with nobody to send to; the script prints the
`psql` query rather than pretending to assert on an absence. **§20's over-context behaviour**
(Decision 10) is likewise unexercised, because producing a conversation longer than the model's
window would mean pasting kilobytes of filler, and a limitation stated is worth more than a
walkthrough that manufactures its own hazard. And section 3's claim is that a **database clock
comparison** found the conversation unmoved, so the script cannot itself manufacture a message and a
summary in the same instant to probe the boundary — the deterministic proof is the integration file,
which scripts the provider and controls both timestamps.

---

## ADR-031 — A draft is a message nobody sent, and accepting it is re-authoring it

**Status:** accepted · Phase W

**Context.** §21 is a five-line workflow and three sentences — *"The AI should generate a draft reply
for the support agent"*, *"AI must NEVER automatically send a customer-facing response in the default
implementation"*, and *"Add an explicit UI distinction between: AI generated draft / human-authored
response."* The chain above them fixes the shape of the phase: `Customer message → Ticket context →
Relevant knowledge → AI → Suggested response → Agent reviews/edits → Agent manually sends`. §41 lists
what a person may do with what comes out of it — *"Allow: regenerate / edit / accept"* — and Phase W's
own block names the four facts to track, *"generated, edited, accepted, regenerated"*. §36 names one
route, `POST /api/v1/tickets/{id}/ai/suggest-response`, between the summary route Phase V built and the
knowledge routes Phase X will.

Everything below the route already existed, and had since Phase D: `SenderType.AI_DRAFT`
(`app/models/enums.py:92`), the `ai_draft_is_internal` CHECK (`app/models/message.py:76-79`),
`AuditAction.AI_RESPONSE_ACCEPTED` and `AI_RESPONSE_REGENERATED` (`app/models/enums.py:142-143`) — both
already named among §34's examples, which is why §41's verbs needed no new action — `AIOperation.
SUGGEST_RESPONSE`, `Permission.AI_REQUEST_SUGGESTION`, `SuggestedReply` (`app/schemas/ai.py:119`),
`AIProvider.generate_response`, and `ai_service.generate_response` with its retry policy, its schema
validation, and its ledger row. **So this phase adds no migration** — the third in a row — and
`alembic check` staying green is the mechanical proof rather than a claim. What Phase W wrote is an
entry point, an instruction, a route that accepts, and the assertions that the customer never sees the
draft and always sees the reply.

Four pieces of scaffolding named this phase and are discharged rather than left standing. `app/ai/
prompts.py` said *"§21's draft reply is what remains, which is why a fourth is not written yet"* — the
fourth is written, and the module now says why there is no fifth: `AIProvider` declares four methods,
so what is left to write is an *input* rather than an instruction. `ai_analysis_service`'s comment on
`ANALYSIS_OPERATIONS` said suggested replies *"are §21 and Phase W"*; the tuple **stays two members** and
the comment now says why (Decision 1). `_execute`'s *"unimplemented operation"* docstring named
`SUGGEST_RESPONSE`; it no longer does. And `app/api/ai.py`'s *"`AI_REQUEST_SUGGESTION` … belong to Phases
W, X"* now reads Phase X alone.

**Decision 1 — the four verbs need no new column, table, or enum member.**

| Verb | Where it lives | Why that is the right home |
|---|---|---|
| **generated** | an `ai_analyses` row, `operation = SUGGEST_RESPONSE`, `status = completed` | `AIAnalysis` is already operation-agnostic and `AIAnalysisRead` already serves `result`, so the model's own output is readable at `GET /tickets/{id}/ai/analyses` with no new endpoint. The request is recorded as `AI_ANALYSIS_REQUESTED` with `metadata = {"operations": ["suggest_response"]}` — the exact shape `request_summary` already writes. |
| **edited** | `AI_RESPONSE_ACCEPTED`'s `before`/`after` | §34 asks for *"before/after values where appropriate"*, and this is the case it was written for: the draft's body against the body that went out. `metadata` carries `draft_id` and `edited: bool`. |
| **accepted** | `AuditAction.AI_RESPONSE_ACCEPTED`, staged in the reply's own transaction | §34 names this member, and the row has to share the commit with the message it describes (§34's trail would otherwise be able to disagree with its data). |
| **regenerated** | `AuditAction.AI_RESPONSE_REGENERATED` | Named at §34, written *instead of* `AI_ANALYSIS_REQUESTED` when a completed draft already exists for the ticket (Decision 6). |

The alternative was a `drafts` table. It was refused because every fact §41 asks to track is already a
fact one of two existing rows has, or a fact the audit trail has: the draft's text and whether it was
edited are the analysis row and the audit row, who sent it is a `messages` row, and "regenerated" is an
action name. A third table would be a second place recording the same four things, and the second place
is the one that goes stale.

`ANALYSIS_OPERATIONS` stays two members and stays §18's. The tuple means *"what one analyze request
does"* — classification and sentiment, run together, writing ticket fields — and a suggested reply is
not that: it is a third entry point over the same worker with its own route and its own row, exactly as
§20's summary is a second one. Adding `SUGGEST_RESPONSE` to the tuple would make "an analysis run" mean
three things and would make `notify_analysis_completed` fire for a draft (Decision 7).

**Decision 2 — the draft is two records, and `app/schemas/ai.py` already says which is which.** That
module's `SuggestedReply` docstring states the split: *"the schema says what the model wrote, and the
sender type says who is claiming it."* So the shape follows the sentence:

- the **`ai_analyses` row** holds the model's own output in `result`, like every other operation;
- a **`messages` row with `sender_type = AI_DRAFT`, `is_internal = true`** holds it in the thread, which
  is where a reply goes and where the agent will read it. §21's *"explicit UI distinction between AI
  generated draft and human-authored response"* is then `MessageRead.sender_type`, a field already on
  the response, and `message_repository.list_for_ticket(include_internal=True)` already returns it to
  staff. No new endpoint, and no flag invented for the UI to read.

The rejected alternative is one record — a message row with an `is_ai` flag — and it fails on the thing
that makes the two records different: they are the *answer* and the *offer*. The analysis row is what
the model said, at a moment, and never changes; the message row is what the agent was shown and may
accept. Collapsing them means accepting has to rewrite the row that holds the model's own output, which
destroys the comparison §34's before/after exists to record, at the exact moment somebody wants to make
it.

**Decision 3 — accepting re-authors the draft, and nothing customer-visible is ever mutated.**
`Message`'s own docstring already said it: *"An AI draft is never customer-visible until an agent sends
it, at which point it is re-authored as an agent message."* Accepting writes a **new** `messages` row
(`sender_type = AGENT`, `is_internal = false`) through the same `post_reply` an ordinary reply uses, and
the draft row stays behind internal and untouched. Nothing customer-visible is mutated, so the
append-only property Phase V's watermark rests on is not approached — and it would not matter if it
were, because `_CONVERSATION_SENDERS` already excludes `AI_DRAFT` from the conversation on both the
watermark side and the prompt side.

Because the reply path is reused and not re-implemented, every side effect a reply has happens for an
accepted draft as well: `first_response_at`, the `MESSAGE_ADDED` timeline entry, `notify_for_event`, the
socket publish, and the analytics cache invalidation. An accepted draft *is* an ordinary reply that
happens to have come from somewhere, and the phase's whole economy is that it costs one branch in
`post_reply` rather than a parallel send path that would have to remember all five.

**There is no message-edit route, deliberately.** §41's "edit" is the client's text box: the agent edits
the draft on screen and sends the edited text, and only the text that went out is persisted. The
difference between the two is recorded in the audit row rather than in a mutable column. This codebase
has no message-mutation path today and does not need its first one to serve a text box — and a mutable
message would be the end of the append-only property the summary watermark depends on.

**Decision 4 — accept is its own route under the ticket, and it carries two capabilities.**
`POST /api/v1/tickets/{ticket_id}/ai/drafts/{draft_id}/accept`, answering `201` and the `MessageRead` it
created. `app/api/messages.py`'s own docstring settles why it is not a `draft_id` field on the reply
body: *"writing an internal note and writing a public reply are different actions, and giving them one
endpoint would mean the endpoint's declared capability no longer described the request."* Accepting is a
different action again — it carries an AI capability and records an AI fact — so folding it into the
reply route behind a field is exactly the move that docstring refuses.

It requires **two** capabilities, `AI_REQUEST_SUGGESTION` and `MESSAGE_POST_REPLY`, because it performs
two actions at once and neither alone describes it: the first is what makes it an AI-lifecycle act, the
second is what makes it the write of a customer-visible message. Today every role that holds the first
holds the second, so nothing turns on it — which is exactly why it should be stated rather than assumed,
and why `tests/security/test_route_protection.py`'s matrix carries both.

A `draft_id` that names nothing on this ticket, or names a message that is not an `ai_draft`, is a
**404** (`AI_DRAFT_NOT_FOUND`) — ADR-009's rule, indistinguishable from a draft that does not exist. A
message that is not a draft is never a draft, and saying *which* is not a distinction a caller outside
the tenant is entitled to.

**Accepting the same draft twice is allowed, deliberately.** The draft row is the offer, not a claim on
the reply; an agent may legitimately send the same text twice, and each acceptance is its own audit row.
Preventing it would need a column that says "used", which is a second place recording what the audit
trail already records.

**Decision 5 — there is no suggestion cache, and Phase V's freshness rule deliberately does not apply.**
A summary is reused when nothing changed because the same input has one right answer. A second request
for a draft is a person asking for a *different* one — §41 lists regenerate as an allowed action, and
Phase W's block lists "regenerated" as a fact to track — so every request makes a real call. There is no
`was_cached` row for this operation and there should not be: the flag exists to measure a saving, and
there is no saving here to measure. Applying the watermark would turn the one verb the spec names into a
no-op, and a user pressing Regenerate and receiving the identical draft is the failure §41 was written
to prevent.

**Decision 6 — a regeneration is recorded in the audit trail, because the response cannot say it.**
There is no `?force=` and no second route, and the reason is that both asks *are* the regeneration. A
first request and a repeat answer `202` with a `pending` row, and both produce a completed draft; the
response cannot distinguish them, and it should not, because asking again is regenerating. So the
difference lives where §34 puts it: `request_suggested_response` reads stored state — whether a
completed `SUGGEST_RESPONSE` row already exists for this ticket — and writes
`AuditAction.AI_RESPONSE_REGENERATED` instead of `AI_ANALYSIS_REQUESTED`. The route decides from stored
state, exactly as `request_summary` decides cached-versus-queued from stored state, and the response
stays one shape.

**A note for anyone reading the trail.** A ticket's creation writes its own `ai_analysis_requested` row
for §18's classification and sentiment, so §21's rows cannot be told apart from §18's by their action
alone. What is unique to a draft request is the operation it names —
`metadata = {"operations": ["suggest_response"]}`, the shape `request_summary` already writes — and that
is what the tests filter on. This is the third route to write `AI_ANALYSIS_REQUESTED` for a different
reason; the action is shared on purpose and the operation is the discriminator. A test that counted the
action would count the ticket's own creation as a draft request, which is how the first version of this
phase's integration test was wrong.

**Decision 7 — the draft is internal, and the database is what says so.** `sender_type = AI_DRAFT` with
`is_internal = true` is not a convention a service is asked to honour: `ai_draft_is_internal` refuses any
draft row written with `is_internal = false`, so the customer-facing half of §21's containment is
structural — the same shape `tickets`' status-transition rules and the AI-review constraints take. The
tests assert it from the customer's own side, because that is the property that matters: the draft must
be **absent from the portal session's own message read**, not merely marked internal in a table the
portal never reads. Asserting on `sender_type` in a query the test wrote would pass even if the
visibility filter were missing entirely.

Two consequences fall out of *"a draft is internal and writes no ticket column"*:

- **A suggestion-only run announces without alerting.** `run_analysis`'s `notify_analysis_completed`
  fires only when an operation in `ANALYSIS_OPERATIONS` completed, and `SUGGEST_RESPONSE` is not a
  member (Decision 1). The alert exists so somebody looks at a ticket whose fields an AI changed; a
  draft changes no field and exists to be read by the person who asked for it, so alerting them would
  interrupt them about a page they are on. The timeline entry and the socket event still happen —
  `_record_completion` writes `AI_ANALYSIS_COMPLETED` for any operation — so the run is quiet rather
  than silent.
- **An empty conversation is not refused**, unlike §20's summary. `create_ticket` writes the description
  onto the ticket and creates no `Message`, so a freshly raised ticket — the case a draft reply is most
  useful for — has a conversation of length zero, and `draft_content` has the description to work from.
  Refusing here would refuse the most ordinary use of the feature.

**Decision 8 — §21's "relevant knowledge" step waits for Phase X, and W's prompt is built from the two
blocks that exist.** The workflow diagram puts retrieval between "ticket context" and the model. That
step is §22's, and the codebase already made this call once by the same reasoning: ADR-008 refuses to
put `generate_embedding` on the provider protocol until Phase X, because a method with no caller is the
stub this codebase refuses everywhere else. A knowledge parameter that W always passed empty would be
that stub. So `draft_content` composes the two builders that exist — `prompts.ticket_content` and
`prompts.conversation_content` — into one user message rather than adding a second implementation of
either, and X adds a third block to one function with its tests. The change is additive because the seam
is a function, which is also why W's instruction is the fourth and last: what remains to be written on
this path is an input, not an instruction.

**What this deliberately does not do.**

- **No knowledge base, embeddings, or retrieval.** §22 is Phase X; `AIOperation.EMBED` and
  `KNOWLEDGE_ANSWER` stay declared-and-unused, per Decision 8 and ADR-008.
- **No automatic sending, ever.** The only path from a draft to a customer-visible row runs through a
  route a person called, and `SuggestedReply` has a body and nothing else — no confidence, no status,
  no sender — so the types cannot express "sent". §41's *"Never make the user believe an AI suggestion
  was written by a human"* is the UI's job, and it is helped by the schema: the draft carries a
  `SenderType` that is not `agent`.
- **No confidence on a draft.** §41 says *"Show confidence where meaningful"*, and an edited draft is
  the case where it is not: the number would describe the model's certainty about text a person is
  about to change. The column stays `NULL`, as Phase V's summaries already left it.
- **No "dismiss" verb.** §41 lists regenerate, edit, and accept; a discarded draft is an `ai_draft` row
  that was never accepted, which is what it looks like.
- **No message-edit route**, per Decision 3.
- **No per-tenant prompt tuning, no temperature or model override on the request**, and no `?force=` —
  the route takes no body at all beyond the ticket, so there is nothing to configure away from
  `AI_MODEL`.

**Cost.** No new dependency, no migration, and no new setting. One entry point
(`ai_analysis_service.request_suggested_response`), one branch in `_execute`, one instruction and one
content builder in `app/ai/prompts.py`, two route functions, two keyword-only parameters on
`message_service.post_reply`, and one repository lookup (`MessageRepository.find_draft`). The standing
cost is the one `app/api/messages.py` already pays and this phase now pays twice: a route that performs
two acts carries two capabilities, so a reader has to check both to know who may accept — which is
cheaper than a route whose declared capability does not describe its request.

The other standing cost is context. A draft's user message is the longest on the path after §20's
summary, because it carries the ticket's two text fields *and* every eligible turn of the conversation;
that is what §21's diagram asks for ("ticket context" and "previous conversation" both), and the
alternative — drafting from the description alone — answers the question that opened the ticket and
ignores everything said since, which is the one thing a reply is most likely to get wrong.

**Verification.** The suite is **1398 tests**, all passing — 1357 at ADR-030's close, so 41 for this
phase's own surface. `alembic check` reports *"No new upgrade operations detected"*, the third phase in
a row to do so and the mechanical proof of the claim above: every column this phase writes — a
`messages` row's `sender_type` and `is_internal`, an `ai_analyses` row's `operation` and `result`, a
ledger row — has existed since Phase D. `ruff check`, `ruff format --check` (195 files), and `mypy app
alembic scripts` (128 files) are clean.

**Three breaks were made deliberately, and each failed the tests that cover it.** They are worth
recording individually, because they fail in three different ways.

*The draft made public.* Flipping the worker's draft row to `is_internal = False` failed **seventeen
tests** across `tests/integration/test_ai_suggestion.py` and `tests/api/test_ai_suggestion.py` — and the
way they failed is the point. Not one failed an assertion; all seventeen failed the same
`psycopg.errors.CheckViolation` on `ck_messages_ai_draft_is_internal`, at the `INSERT`. §21's
containment is a database constraint, and this is what that looks like from a test: the row is never
written, so every test that expected a draft to exist reports a *missing* row rather than a leak. A
service-level check would have produced the leak — and there is no service-level check, which is why
this is the shape the break takes.

*The accept that sends the draft itself.* Replacing the new reply row with the draft row —
`message = draft if draft is not None else _post(...)` — failed **seven** tests, and the first is the
one the phase exists for: `test_the_customer_sees_the_reply_and_never_the_draft`, because a "reply" that
*is* the internal `ai_draft` row reaches no customer at all. `test_the_draft_row_itself_is_untouched`,
`test_accepting_sends_the_text_as_the_agents_own_reply`, `test_accepting_the_same_draft_twice_sends_twice`,
and three API tests failed with it. This is the whole reason Decision 3 says **re-authored** and not
*marked*.

*The regeneration dropped.* Removing the branch so `request_suggested_response` always writes
`AI_ANALYSIS_REQUESTED` failed **exactly one test** —
`test_the_first_request_is_audited_as_requested_and_the_second_as_regenerated` — and left the rest of
the file green. That is the right shape for a decision whose only observable is an audit row: §41's
regenerate has no response to inspect (Decision 6), so the one test that reads the trail is the one test
that can fail. A break that took more of the file down would mean the trail were load-bearing for
something it is not.

Each break was reverted, and the reverts confirmed by re-running the affected files green (61 tests,
0 failures).

**The live end-to-end ran against a real key.** `uvicorn` under
`app.core.event_loop:loop_factory`, a Celery worker on `--pool=solo -Q notifications,sla,ai`, and
`scripts/phase_w_walkthrough.py` raising a real ticket with a real conversation, asking for a draft over
HTTP, and accepting it — all of it against the real configured model:

    provider=groq model=openai/gpt-oss-120b

    1. §21 sentences 1-2: a ticket with a conversation, and nothing drafted yet
      ok    the ticket was created
      customer: Ada Lovelace  ticket: 1
      the ticket's own §18 analysis settled: completed
      ok    a customer message and an internal note were posted
      the note is staff-only, and it is in the prompt -- the model reads it as context.
      ok    no draft exists yet
      ok    and no ai_draft message is in the thread
      queued: suggest_response  status=pending
      ok    the route accepted the request
      ok    it answered with one row, queued or already done
      ok    the row names the model that will be asked
      ok    the response is not the reply -- nothing has been sent

    2. §21 sentence 1: the worker drafts it, in another process -- and it is staff-only
      settled after 2.1s: status=completed
      ok    the draft completed
      draft: 'I'm sorry you're unable to download your policy document. Our file storage
      service is currently experiencing degraded performance and is returning 503 errors,
      which is why the download isn't starting. Our engineering team is actively working
      on a fix. I'll let you know as soon as the service is back up and the document can
      be downloaded. Thank you for your patience.'
      ok    it is a real reply somebody could send
      ok    the row carries no confidence
      ok    the call was billed for its tokens
      ok    the draft changed nothing on the ticket
      ok    the worker wrote one ai_draft row into the thread
      ok    it is internal, and it was written by nobody in the tenant
      ok    its body is the model's own text
      ok    the customer's own read does not contain the draft
      ok    and it does not contain the draft's text
      ok    but the customer's own message is still there, so this is a filter and not an empty list
      thread as staff: 3 rows, one of them the draft.
      thread as the customer: 1 rows, none of them the draft.

    3. §41's regenerate: asking again is a second draft, and the trail says which
      ok    no regeneration has been recorded yet
      queued: suggest_response  status=pending
      ok    a second row was queued rather than the first one served
      ok    and it is a fresh one
      ok    the second ask is recorded as a regeneration
      ok    and it names the operation, not just the ticket
      ok    the first ask is still recorded as a request
      ok    the regenerated draft completed
      draft: 'I'm sorry you're unable to download your policy document. Our system is
      currently experiencing a service issue that is affecting file downloads and
      returning a 503 error. Our engineering team is aware of the problem and is working
      to resolve it. I'll let you know as soon as the download functionality is back up.
      Thank you for your patience.'
      ok    both drafts are in the thread
      the superseded draft is still there -- §6 keeps it for comparison, and the read
      route serves the newest row per operation, which is why only one shows above.

    4. §41's accept: a person sends it, and the draft stays behind
      accepting draft 6ce4c5a7-ee14-4882-b9e0-ce17f5afc4d2
      before: calls=4 cached=0 cost=0.000748
      ok    the acceptance answered 201
      ok    it is a public message
      ok    and it is the agent's own reply, not the model's
      ok    carrying the text the person wrote
      after:  calls=4 cached=0 cost=0.000748
      ok    the acceptance made no model call
      ok    and spent nothing at all
      ok    the draft row is still in the thread
      ok    and it is unchanged -- same body, still internal
      ok    the customer now has the reply
      ok    and still no draft
      ok    and still not the model's text
      ok    the acceptance is in the audit trail
      ok    it records what the model offered
      ok    and what actually went out
      ok    and that the agent edited it
      ok    and which draft it came from

    5. §53: the same ticket id, asked by another tenant
      ok    the stranger's request is a 404
      ok    and it names no ticket
      ok    the owner still reads their own drafts

    46 passed, 0 failed

Sections 2 and 4 are the phase in two lines each, and they are the same line twice: *thread as staff:
3 rows, one of them the draft* against *thread as the customer: 1 rows, none of them the draft*. The
containment §21 asks for is not a rule anywhere in the application — the customer's **own portal
session** asks for the thread and the filter answers, and section 4 repeats it after the reply has gone
out so that the absence cannot be read as "nothing has happened yet". Section 4's `cost=0.000748` is
identical either side of the acceptance, to the cent: §41's accept cost no model call because a person
wrote the text and the model was paid for when the draft was asked for.

The second draft in section 3 is a different piece of writing from the first, which is Decision 5 in the
only form a reader can check it: the same ticket, the same conversation, a second call, and a
different answer. Had Phase V's freshness rule been applied here the two blocks would be the same
sentence and the verb §41 names would do nothing.

One thing the run found rather than confirmed: the second response opens *"Our system is currently
experiencing a service issue"* where the first said *"Our file storage service is currently
experiencing degraded performance and is returning 503 errors"*. Both are grounded in the agent's
internal note — the note is what told the model the file store returns 503s — which is Decision 3 of
ADR-030 still holding on this path: the draft prompt reads people, the note included, and the note is
where the actual cause was written down.

**The notification absence was then checked in the database rather than left as an argument:**

    notifications_for_that_ticket
    ------------------------------
                                0
          event_type       | count
    -----------------------+-------
     created               |     1
     message_added         |     2
     internal_note_added   |     1
     ai_analysis_completed |     3

Three `ai_analysis_completed` timeline entries — the ticket's own §18 run and the two drafts — and zero
notifications, which is Decision 7's *"quiet rather than silent"*: the entry is written by
`_record_completion` for any operation, and the alert is skipped because `SUGGEST_RESPONSE` is not in
`ANALYSIS_OPERATIONS`. **That zero is weaker than it looks, for ADR-030's reason and worth restating:**
this ticket was never assigned, and `notify_analysis_completed` sends to the assignee and to managers,
so §18's run would have produced nothing either. What actually carries Decision 7 is
`test_a_draft_only_run_announces_without_alerting`, which assigns an agent, runs a draft-only
suggestion, and asserts the notification list is empty anyway.

The script's own closing note records four things it cannot show for itself, and all four are covered
elsewhere rather than papered over. **The notification that is *not* sent** has no HTTP surface at all,
so the script prints the `psql` query above rather than pretending to assert on an absence. **§41's
`edited: false` case** would need a second, redundant reply to the same customer — the script edits
deliberately — and is asserted instead in `tests/integration/test_ai_draft_acceptance.py`, which
controls both bodies. **§21's "relevant knowledge" step** is Phase X's and is recorded as absent rather
than stubbed (Decision 8). And **a draft of a ticket nobody has replied to** cannot be staged here
because every ticket the script raises has a conversation; the scripted provider in the integration
suite makes that case deterministic, which is where it is tested.

## ADR-032 — A knowledge base is only as good as what it refuses to answer

**Status:** accepted · Phase X

**Context.** §22, §23, and §24 are one feature in three parts, and the build specification calls it a
*"flagship feature"*: *"RAG KNOWLEDGE BASE"*, *"RAG QUERY FLOW"*, and *"RAG HALLUCINATION CONTROL"*.
It is the last AI capability the system was designed around and never built — every other operation
answers from priors, and §22 is what lets an answer be grounded in the organization's own documents.
`docs/architecture.md` §7 has carried the intended flow since Phase B, in the present tense and
unbuilt.

**Every layer below the service already existed, and had since Phase D.** The baseline migration
creates the `vector` extension, both tables, and the HNSW index; the models are fully built; the
enums, permissions, and audit actions are all declared. **So Phase X adds no migration — the fourth
phase in a row** — and `alembic check` reporting *"No new upgrade operations detected"* is the
mechanical proof rather than a claim.

| Already declared | Where | Says what |
|---|---|---|
| `KnowledgeDocument` | `app/models/knowledge_document.py` | `is_published`/`status` are independent; a partial index on `is_published = true AND status = 'completed'`; a trgm index on `title` awaiting an admin title search |
| `KnowledgeChunk` + HNSW | `app/models/knowledge_chunk.py` | `EMBEDDING_DIMENSIONS = 1536`; `vector_cosine_ops`; chunks carry `organization_id` **so a tenant-filtered search needs no join** |
| `ProcessingStatus`, `DocumentSourceType` | `app/models/enums.py` | `pending/processing/completed/failed`; `upload/url/manual` |
| `AIOperation.EMBED`, `KNOWLEDGE_ANSWER` | `app/models/enums.py` | unused |
| `AuditAction.KNOWLEDGE_DOCUMENT_CREATED/DELETED` | `app/models/enums.py` | unused |
| `Permission.KB_LIST/KB_UPLOAD/KB_DELETE/AI_QUERY_KNOWLEDGE` | `app/core/permissions.py` | granted: admin all; manager and agent list/read/query; customer none |

Six scaffolding comments named this phase and are discharged by it rather than left standing.
`provider.py`'s *"Phase X adds the method with the provider that can answer it"*; ADR-027 Decision 1's
*"Phase X adds `generate_embedding` together with the provider that can answer it"*; `celery_app.py`'s
*"the queues that will join them are `reports` (S) and `knowledge` (X) — neither of which is declared
here"*; `ai_analysis_service._execute`'s *"§22's `KNOWLEDGE_ANSWER` is already declared and
unimplemented"*; ADR-031 Decision 8's *"§21's knowledge step waits for Phase X"*; and
`docs/architecture.md` §7.

**Decision 1 — §17's five methods are served by two protocols, and §23's answer is a sixth on the
first.**

`app/ai/provider.py` now declares three shapes. `Provider` is the super-protocol — one attribute,
`name`, the value the ledger records — so `ai_service._run` can be typed against something both kinds
of call satisfy, and a union would be a retry loop with a branch in it. `AIProvider` carries the
generation methods. `EmbeddingProvider` carries `generate_embedding` alone.

**ADR-027's deferral is discharged rather than reversed.** It refused to declare the method because
`ClaudeProvider` would be forced to carry one it could never serve. That argument did not stop being
true when the vendor arrived — it applies to *two* providers now, since neither Anthropic nor Groq
publishes an embedding model — so the method lands on the protocol whose implementations can answer
it. The alternative, one `AIProvider` with five methods and two implementations that raise
`NotImplementedError` on one of them, is the stub this codebase refuses everywhere else.

**§23's answer is not a seventh protocol.** It is prose plus the passages it used, which is a
generation call and nothing else: every generation vendor serves it exactly as it serves the other
four, through the same tool-call mechanism and the same `validate_output` gate. A protocol exists
where implementations *differ*, and there is nothing here for one to differ about. §17's own word for
its list is *"example"*, which is what makes the sixth name a compliance rather than a departure.

`AIResult[T]` is **reused unchanged**. An embedding response reports `prompt_tokens` — the vendor's
own count of what it embedded — `completion_tokens = 0` because nothing was generated, and
`cost_usd` prices that with `output_per_mtok = 0` reproduced exactly. No new ledger mechanics, no new
column, no second retry loop: a genuine second vendor runs through the identical `_run` that Phase T
wrote for the first.

**Decision 2 — the embedding request is a list of texts, not an `AIRequest`.** `AIRequest` carries an
`instruction`, a `content_label`, and a `max_tokens`; an embedding call has none of the three, and
handing it one would mean inventing an instruction for a model that does not read one. So
`ai_service._run` is generalized over its request type — `_run[RequestT, T: BaseModel]` — and gains
one keyword-only `model` parameter, because an embedding call is priced and recorded under
`EMBEDDING_MODEL` rather than `AI_MODEL`. Everything else about `_run` is reused as-is: §17's retry
policy, the jittered backoff, one ledger row per attempt, and the rule that it never commits.

**Embedding is batched, one call per `EMBEDDING_BATCH_SIZE = 64` passages.** The vendor bills the
same tokens either way, and one call is one timeout instead of forty — which matters because the
caller is a Celery task ingesting a document rather than a person watching a spinner. One ledger row
per batch is the honest unit: a batch is one request to a vendor and one line of spend.

**Decision 3 — `is_published` is set by the ingestion worker, and §22 says why.** §22's pipeline ends
*"document becomes searchable"*, and the model's own partial index is
`is_published = true AND status = 'completed'`. So a successful ingestion sets `status = COMPLETED`,
`processed_at`, `chunk_count`, and `is_published = true`.

**No create-payload field and no publish route.** A field whose `false` value nothing can undo is a
trap, and §22's checklist for this phase is *"document upload, extraction, chunking, embeddings,
pgvector, semantic search, grounded generation, source references"* — withdrawing a document is not
on it. `knowledge_document.py`'s `is_published` comment, which said *"not exposed before review"*, is
rewritten to the truth: the column is independent of `status` so a later phase can pull a document
out of retrieval without deleting it, and this phase does not build that.

There is a second consequence, and it is the one that keeps publishing out of the audit trail. §34's
list has exactly two knowledge actions, and this phase implements exactly those two — so *publishing*
is not audited, and it is the worker's job rather than a route's precisely because a route that
published would be an unaudited mutation performed by a person.

**Decision 4 — three source kinds, and upload is its own route because it is a different transport.**

| Route | `source_type` | What the server does |
|---|---|---|
| `POST /api/v1/knowledge` | `manual` or `url` | the text is already in the body, or is fetched and extracted |
| `POST /api/v1/knowledge/upload` | `upload` | validate, store the object privately, extract in the worker |

`KnowledgeDocumentCreate` takes `title` plus **exactly one** of `content` / `url`, enforced by a model
validator, and `source_type` is a derived property rather than a field. **The kind follows from the
body rather than being declared in it**, so the body cannot say two things and a client cannot send
`source_type=manual` alongside a URL. The act is the same act either way — register a document and
hand it to the ingestion pipeline — which is why it is one route and not two.

Upload is a route of its own because it is a different transport: a multipart body the server must
validate and store, which is the distinction `app/api/attachments.py` already draws and the reason
`POST /knowledge` takes JSON and only JSON. **The original filename is not persisted**: the schema has
no column for it, the required `title` is the document's name, and a stored client filename is a
second name for the same thing that nothing reads.

**Decision 5 — ingestion is a pipeline of pure functions plus one worker.** Three new modules, split
by what each is allowed to touch:

- **`app/services/document_text.py`** — no database, no network, no vendor. `extract`, `clean`, and
  `chunk`, every one a pure function of its arguments, which is what lets the chunker be tested
  without a fixture or an event loop and what makes re-chunking a function call rather than a
  re-ingestion. Three readers for three formats: `pypdf` for PDF, a small `html.parser.HTMLParser`
  subclass for HTML, and a decode for everything else in `READABLE_MEDIA_TYPES`. A type outside that
  set is refused rather than decoded into mojibake, which is the answer an image deserves. The HTML
  reader is hand-written for the reason `app/core/file_validation.py` gives for hand-writing signature
  tables: the job is to walk a tag stream and drop `script` and `style`, BeautifulSoup does far more,
  and a native-free dependency that does exactly this is cheaper than a library nobody can be sure is
  not rendering something.
- **`app/services/knowledge_service.py`** — the rules, and the only module that knows what a document
  row means. `create_manual`, `create_from_url`, `create_upload`, `list_documents`, `get_document`,
  `delete_document`, `enqueue_ingestion`, `run_ingestion`, `retrieve`, `answer`.
- **`app/workers/knowledge_tasks.py`** — the `ai_tasks.py` shape exactly: a module-level `NullPool`
  engine, `event_loop.run`, ids as strings, and **no retry at the task level** because §17's policy has
  already been applied to every call the task makes. Redelivery is handled by the row's status.

**Chunking targets ~800 estimated tokens with ~100 of overlap.** `CHUNK_TARGET_TOKENS` is the size a
passage should be: too small and a passage cannot contain an answer, too large and one vector has to
stand for several subjects at once. `CHUNK_OVERLAP_TOKENS` exists because a boundary is drawn by
arithmetic rather than by meaning — a sentence stating a condition can sit exactly across one, and
neither half alone would retrieve — and it is kept small because those tokens are embedded twice and
billed twice. `_MAX_SEGMENT_TOKENS = CHUNK_TARGET_TOKENS - CHUNK_OVERLAP_TOKENS` is what keeps the
invariant in `chunk` provable rather than merely likely.

`token_count` holds an estimate from the four-characters-per-token ratio OpenAI publishes for English
prose, and it is stated as what it is: **a number that sizes a chunk, never a bill.** The billed
counts are the provider's own and the ledger records those. **`tiktoken` was considered and
rejected**: it downloads its BPE file on first use, which would make the first ingestion of a fresh
deployment depend on a third party's CDN being up, and a chunk boundary is not a place where
exactness buys anything. The target is conservative by roughly four times against
`text-embedding-3-small`'s 8191-token input limit, so an estimate that is wrong about a pathological
document still cannot exceed it.

`MAX_CHUNKS = 500` is the per-document cap, and reaching it **fails** the document rather than
truncating it: a document silently indexed halfway answers nothing past the cut, and nothing would say
so. The cap exists because a document that grew without one would spend an unbounded amount of the
organization's money on embeddings in a single task.

**Failure is contained, in both directions.** A provider that is down, a fetch that is refused, and a
document that yields no text all end the same way: `status = FAILED` and an `error_message` holding
the *reason*, never a provider's raw payload — `app/ai/errors.py`'s existing rule, and the reason is
rendered to an admin, so it is the same class of text a client may see. And a document that yields no
text is **an error rather than an empty success**: a scanned PDF extracts to nothing, and a
`completed` document with no chunks would answer nothing and look fine.

**A row that is not `pending` is skipped, and that is stricter than `run_analysis`.** There, `pending`
and `processing` are both claimable; here one delivery owns the document, because the alternative is
two workers appending the same chunks and colliding on `uq_knowledge_chunks_document_id_chunk_index`.
A crash mid-ingestion therefore leaves a document in `processing` that nothing will pick up, and the
recovery is the delete route and a fresh upload — which is honest, visible in the list, and not a
silent half-ingestion. The row is committed as `processing` *before* the first network call, so
"running" is a state another process can see, which is what makes a redelivered task a no-op rather
than a second embedding bill.

**Decision 6 — a feature that fetches a URL for a user is an SSRF primitive, and the guard is the
address.** `app/services/url_fetch.py` fetches a `url` document. The API can reach addresses the
person asking cannot: `http://localhost:8000/…` is the deployment's own admin surface,
`http://169.254.169.254/` is a cloud metadata endpoint that hands out credentials, and
`http://10.0.0.5/` is whatever is inside the private network the service runs in. So the guard is not
an extra.

**The guard checks the address, not the name.** A blocklist of hostnames is defeated by a name that
resolves to `127.0.0.1`, and a check applied only to the URL the client sent is defeated by a public
page that redirects to the metadata endpoint. So **every hop is resolved with `getaddrinfo` and every
address it resolves to must be a globally routable one** — a name resolving to both a public and a
private address is refused rather than gambled on, and refusal happens on the hop that offends rather
than on the one that started it. Redirects are followed **by hand**, because a redirect is exactly
where a checked URL becomes an unchecked one: `follow_redirects=True` would move the request before the
guard could look at where it went. The hop count (`MAX_REDIRECTS = 5`), the body size
(`MAX_URL_BYTES = 5 MiB`), and the final URL's length (`MAX_URL_CHARS = 1000`, the column's width) are
all bounded, and the content type is checked against `document_text.READABLE_MEDIA_TYPES` — imported
rather than restated, because two copies of "what can be read" is how a page comes to be fetched as a
text document and refused as an unreadable one a second later.

**What the guard does not close, stated plainly.** `getaddrinfo` and the connection that follows it are
two lookups, so a name whose answer changes between them — DNS rebinding — can still reach a private
address. Closing that needs the connection pinned to the address that was checked, which `httpx` does
not expose; it would mean hand-rolling the socket. The guard therefore raises the cost of the attack
from "spell a hostname" to "control a DNS server and win a race", and the honest thing is to write that
in the module rather than to describe the guard as complete.

**The fetch happens in the worker, never in the request.** The row is written with the URL as its
reference and the worker fetches it under the guard, so a request cannot be made to wait on a
stranger's server — §16's *"the API should not wait unnecessarily"* satisfied structurally, and the
`201` is honest about it because `pending` is on the row.

**Decision 7 — the question path retrieves, answers, or says the base lacks it.**

`POST /api/v1/knowledge/search` embeds the question (`AIOperation.EMBED`, a real call in the request
path, because a question is asked by a person who is waiting), searches published and completed chunks
**scoped to the caller's tenant**, and then takes one of two branches.

**Nothing clears the threshold → no answer call at all.** The response is §24's sentence, written by
this server, with `sources: []` and **no ledger row** — `was_cached` would be a lie, because nothing
answered from a cache and the call was never going to be made. Cheaper and strictly more honest: there
is no model in the loop to answer from its priors, and nothing that could fabricate a citation.

**Something clears it → one grounded call.** `prompts.KNOWLEDGE_INSTRUCTION` is §24's four rules made
concrete, and `prompts.knowledge_content` numbers the passages into one fenced block. The threshold is
`RETRIEVAL_MIN_SIMILARITY` and it is applied **by the query, not afterwards**: filtering the top `k` in
Python would answer a different question — "of the `k` nearest, which clear the bar" — and would return
fewer rows than asked for. An empty return therefore means "nothing was close enough" rather than
"nothing was found", and the caller answers both the same way, because §24's refusal is the honest
response to either.

**The `EXISTS` guard exists so a tenant with nothing to search pays nothing.** `retrieve` asks
`has_published_chunks` before it embeds anything: one indexed `LIMIT 1` read answers whether retrieval
*could* return anything, so an organization that has never ingested a document never pays for a vector
it cannot use. §53 names repeated AI calls as waste, and a call that cannot succeed is the purest form
of it. `embedding IS NOT NULL` is part of that question rather than defensive — a chunk row is written
before its vector is known, and a predicate this function did not share with `search_chunks` would let
a tenant with only half-ingested rows pay for an embedding to search a set the search would then return
nothing from.

**Citations come from the retrieval, never from the model's prose.** `used_sources` are 1-based indices
into the passages actually supplied; `_cite` maps them to those chunks and **drops any index outside
that range with a log line**. Dropping rather than failing, because dropping is not fabricating and a
model that cited `[7]` when handed three passages still produced a usable answer. The result is ordered
by descending similarity rather than by the order the model typed, and de-duplicated — a passage named
twice is one passage. `prompts._citable_passages`'s own comment notes that a passage's text can contain
a line shaped like a citation number, which is what makes the mapping, rather than the prose, the thing
§24's *"do not fabricate citations"* rests on.

**No `grounded` boolean.** `sources == []` is the whole of that fact, and two fields for one fact is
how they come to disagree.

**Decision 8 — §21's drafts retrieve too, and retrieval there is fail-open.** `prompts.draft_content`
gains a third block — the additive change ADR-031 Decision 8 forecast, landing in one function with its
tests — and `_draft_request` passes it. The query is the ticket's own words, subject and description;
sentiment is not in it, because retrieval is a similarity search over documents and "frustrated" is not
a phrase a policy page contains.

**A knowledge outage must not stop drafting.** `_knowledge_for_draft` catches `AIServiceError`, logs it
with its type, and returns `[]`, so the draft proceeds on the two blocks that already exist. A drafting
feature that dies when retrieval is down is worse than a draft that is merely less grounded. **The
catch is deliberately narrower than `run_analysis`'s per-operation one**: a database error or a bug in
the retrieval query still propagates, because swallowing those would hide a real fault behind a reply
that merely cites nothing.

**And a tenant with no published chunks makes no embedding call at all**, because `retrieve`'s guard
runs first — which is what keeps every existing §21 test making exactly the calls it made before, and
keeps a tenant that has never opened the knowledge base from paying for a vector they cannot use. The
block is unnumbered: a draft cites nothing, so `[3]` in a reply an agent may send to a customer is
noise at best and a leaked internal reference at worst. `_DRAFT_LABEL` widened to name the third block,
with "any" rather than "the" because the overwhelming majority of drafts have none.

**Decision 9 — six routes, one new error code, no new permission.**

| Route | Capability | Notes |
|---|---|---|
| `POST /api/v1/knowledge` | `KB_UPLOAD` + `limit_upload` | 201 + `KnowledgeDocumentRead`; queues ingestion; audits `KNOWLEDGE_DOCUMENT_CREATED` |
| `POST /api/v1/knowledge/upload` | `KB_UPLOAD` + `limit_upload` | multipart; the object is stored before the row commits |
| `GET /api/v1/knowledge` | `KB_LIST` | paginated; `q` filters the title |
| `GET /api/v1/knowledge/{document_id}` | `KB_LIST` | the matrix has no "view document" row, so reading takes the list capability — as `GET /customers/{id}` does |
| `DELETE /api/v1/knowledge/{document_id}` | `KB_DELETE` | deletes the row (chunks cascade by the FK) **and** the stored object; audits `KNOWLEDGE_DOCUMENT_DELETED` |
| `POST /api/v1/knowledge/search` | `AI_QUERY_KNOWLEDGE` + `limit_ai` | the grounded answer |

**A query writes no audit row**, because §34's list has no knowledge-query entry and a search is not a
mutation. §3's rows for all four knowledge capabilities are already in the role sets, so
`app/core/permissions.py` does not change and `tests/unit/test_permissions.py` proves the matrix still
matches `docs/requirements.md`.

**`DELETE` is the first delete route in the system, and its ordering is deliberate.** The object goes
first, then the rows: an object whose row is gone is unreachable and a lifecycle rule can collect it,
while a row whose object is gone is a document an admin can see and cannot re-ingest. Storage refusing
with a 503 means nothing is deleted, so the deleting does not half-happen. Only an `upload` document
has an object — a `manual` one has no `source_reference`, and a `url` one has the client's own URL,
which is not storage's to remove. `delete_object` is idempotent because S3's delete is, which suits the
one caller: a retried deletion should not fail the request that is trying to clean up.

**`ErrorCode.KNOWLEDGE_DOCUMENT_NOT_FOUND` is added**, the one gap in a vocabulary whose own docstring
claims to be complete. A document in another tenant is a 404, indistinguishable from one that never
existed — ADR-009's rule.

`GET /knowledge?q=` filters the title with an `ILIKE` and is the consumer for the trgm index Phase D
declared as *"Title search in the admin list view"*; without it, that index is a promise nothing keeps.

**Decision 10 — one dependency and six settings, each with a consumer.** **`pypdf`** is one pure-Python
dependency, for the case §22 names first: *"product documentation"* and *"refund policies"* are PDFs,
and `file_validation.ALLOWED` already admits `application/pdf`, so a user will upload one.
`text/markdown` joins `ALLOWED` beside `text/plain` (`.md`/`.markdown`) — a policy written in markdown
is plain text, the IANA registry gives it its own type, and the attachment table inherits the entry
because the table is shared, which is stated rather than accidental.

Exactly six settings, and the count is a decision:

- **`EMBEDDING_PROVIDER`** (`Literal["openai", "fake"]`), **`EMBEDDING_API_KEY`**, **`EMBEDDING_MODEL`** —
  the `AI_*` trio mirrored, and for the reason that block already gives: *"One key, not one per vendor…
  A second variable would be a setting one of the two providers never reads."* An embedding vendor is
  genuinely a second vendor (ADR-008), so it needs its own three. `EMBEDDING_API_KEY` joins the
  `_blank_credential_is_absent` validator; `fake` is gated to `ENVIRONMENT=test` exactly as
  `AI_PROVIDER` is (§60); and the model/provider pairing joins `_the_model_is_served_by_the_provider`,
  which is the one place a 401 becomes a startup error rather than a failed ingestion.
- **`RETRIEVAL_TOP_K = 5`** and **`RETRIEVAL_MIN_SIMILARITY = 0.3`** — §24's threshold is a policy about
  when this product is allowed to answer, and where a policy lives is a decision worth stating.
  `KnowledgeQuestion` therefore has **no `top_k`**: a client that could raise it would be spending the
  organization's budget on a bigger context, and the number that decides whether an answer is allowed
  at all is not one a caller should be able to move.
- **`MAX_KNOWLEDGE_DOCUMENT_BYTES = 10 MiB`** — the upload ceiling, mirroring `MAX_ATTACHMENT_BYTES`,
  and the knob that bounds what one document can cost to embed.

`app/ai/pricing.py` gains `text-embedding-3-small` as `ModelRate("openai", Decimal("0.02"),
Decimal("0"))`, so `cost_usd` prices an embedding call with no new arithmetic. `.env.example`'s
`--- AI ---` block gains the three keys with no values.

**Decision 11 — the vendor is one module, and the one status it reads differently matters.**
`app/ai/openai_embedding.py` follows `app/ai/groq.py` exactly: a per-loop cached `httpx` client, no SDK,
this project's own timeout, `max_retries=0` because the retry loop is `ai_service`'s, a `429`/`5xx`/timeout
mapped to `AITransientError` and anything else to `AIPermanentError`, a vendor's own error message never
passed along, and `validate_output(Embedding, payload)` as the single §18 gate.

**A `429` whose `error.code` is `insufficient_quota` is permanent, and it is not a detail.** Groq's 429
means "slow down" and retrying is exactly right; this one means the account is out of credit and cannot
succeed however long the caller waits, so treating it as transient would spend the whole attempt budget —
three sleeps and three refused requests — to arrive at the same failure. The code is checked before the
status, and one allowlist query buys a faster and more accurate failure.

**The response's `index` is what orders the vectors, and the count is checked against the number of
texts sent** — the one shape check a schema validator cannot make, because it cannot see the request. A
caller is about to write those vectors against the passages it split a document into, and one vector
attached to the wrong passage is an answer that reads fluently and cites the wrong policy; nothing
downstream could detect it. `_embed`'s `zip(..., strict=True)` is the second half of that guarantee.

`Embedding` lives in `app/schemas/knowledge.py` rather than beside the four generation schemas, and the
reason is its validator: the one thing that can be wrong with a vector list is its width, the width is
a fact about this package's storage, and importing `EMBEDDING_DIMENSIONS` makes the two declarations
one. A 3072-dimension vector is an `AIOutputError` here rather than a driver error at insert. Per
`_failure_summary`'s rule the number is on the chained `ValidationError` rather than in the reason, so
the failure reads `did not match Embedding: <root>: value_error` and the cause says *"a vector was 3072
wide; the column is 1536"*.

**No fence on an embedding call, and its absence is deliberate.** The generation providers wrap
untrusted text because a document can contain instructions and a language model reads instructions. An
embedding model does not: the `input` field is data to be mapped into a vector, and there is no
instruction in the request for a document to hijack. Applying `as_untrusted` here would put fence
markers *into* the embedded text, which would change the vector for no gain.

`FakeEmbeddingProvider` joins `app/ai/fake.py` with **two modes**, because the two answer questions
that need different things from a vector. Hashed — every text embedded by a deterministic function of
its own bytes — is the right double for everything about ingestion, where the property is that the rows
were written in order with the right width and the model's name on them, and the wrong one for
retrieval, where a test would be asserting a property a hash cannot have. Scripted — a mapping from
text to a chosen vector — is how a test makes similarity a *decision*. A scripted text that is not in
the mapping **raises** rather than falling back to the hash, because a silent fallback is what turns a
ranking assertion into a coincidence.

**What this deliberately does not do.**

- **No migration, and no new enum member.** §34's list has exactly two knowledge actions and this phase
  implements exactly those two, so publishing is not audited — which is why it is the worker's job and
  not a route's.
- **No unpublish, no rename, no `PATCH`.** `is_published` is written once, by the worker, when ingestion
  succeeds; withdrawing a document means deleting it, which is audited. The column's independence from
  `status` is what a later phase's editorial switch is for.
- **No re-embed sweep.** `embedding_model` on each chunk is what makes one possible after a model
  change; this phase writes the column and does not read it.
- **No OCR.** A scanned PDF extracts to nothing, and that lands as a *failed* document whose reason says
  the extraction produced no text — not as an empty document that answers nothing and looks fine.
- **No hybrid search, no re-ranking, no query rewriting.** One vector query, one threshold, one prompt —
  §22's flow, and the spec's.
- **No streaming answers, no answer cache.** §23 asks for an answer and its sources.
- **No document text in the read representation.** `content` is retained so a document can be
  re-chunked, and `source_reference` is withheld because for an upload it is an object-storage key —
  the argument `app/schemas/attachment.py` makes about `storage_key` applies unchanged. A document's
  readable surface is its title and its chunks, and a list of twenty documents should not carry twenty
  documents' worth of text.
- **No conversation-aware question rewriting.** §23 asks a question; the retrieval does not read the
  ticket.

**Cost.** One dependency (`pypdf`), no migration, six settings, and one new error code. The standing
cost is the one a second vendor always carries: `Settings` now validates **two** model/provider pairings
rather than one, so a deployment can be misconfigured in a way that only the embedding half notices —
which is why the pairing check is a startup error rather than a failed first ingestion. The other
standing cost is the chunker's two numbers, which decide this feature's retrieval quality and are
therefore the two worth arguing about; they are module constants rather than settings because
re-chunking after a strategy change is possible at all only because the model retains `content`.

**Verification.** The suite is **1584 tests**, all passing — 1398 at ADR-031's close, so 186 for this
phase's own surface. `alembic check` reports *"No new upgrade operations detected"*, **the fourth phase
in a row** to do so and the mechanical proof of Decision 5's claim: every column, index, and enum member
this phase writes — both tables, the HNSW index, `ProcessingStatus`, `DocumentSourceType`,
`AIOperation.EMBED`, `AIOperation.KNOWLEDGE_ANSWER`, and the two `AuditAction`s — has existed since
Phase D. `ruff check` is clean, `ruff format --check` covers 212 files, and `mypy app alembic scripts`
covers 137.

**The full suite earned its runtime by failing.** The first complete run reported **two failures** in
`tests/unit/test_ai_analysis_service.py`: both call `_draft_request` directly, Phase X added its third
parameter, and neither had been updated — `TypeError: _draft_request() missing 1 required positional
argument: 'knowledge'`. They are the failing kind of test rather than the missing kind, which is the
better failure, but the phase's own files were green and only the whole-suite run reached them. Both
were updated to build all three blocks and to vary the passage list with the conversation, since it is
the other one that may legitimately be empty; the file is green at 13 tests, and the suite above is the
re-run.

**Three breaks were made deliberately, and each failed the tests that cover it.** They are worth
recording individually, because they fail in three different ways.

*The tenant predicate removed from the retrieval query.* Deleting **both** arms — the chunk's
`organization_id` and the document's — failed **exactly one test of 55**,
`test_a_nearest_neighbour_in_another_tenant_is_never_returned`, and it failed on the assertion the phase
exists for: `assert [source["document_id"] for source in body["sources"]] == [north_document]` →
*At index 0 diff: `<southwind's id>` != `<northwind's id>`*. The other tenant's passage is not merely
present, it is **first**, because that test scripts its vectors so that it is the nearest neighbour in
the whole table by arithmetic. Worth recording alongside: removing **either arm alone** still passes.
The two clauses are redundant on purpose — a chunk carries its own `organization_id` so a
tenant-filtered search needs no join — and that redundancy is exactly what makes a single-arm break
survivable, which is why the break recorded here removes both.

*The citation range guard removed.* Replacing `_cite`'s `1 <= number <= len(matches)` check with a direct
`matches[number - 1]` — the naive implementation that trusts the model — failed **two tests of 40** in
`tests/api/test_knowledge.py`, both with `IndexError: list index out of range` at the mapping, and both
logged as `unhandled_exception` on `POST /api/v1/knowledge/search`. `test_a_question_with_a_retrieved_
passage_is_answered_and_cited` is the one written for it: its model names passages `[1, 9]` when there is
one passage. Decision 7 says dropping is not fabricating and that a model miscounting still produced a
usable answer; this is what the alternative looks like from a client — a `500` instead of a citation.

*The `EXISTS` guard removed from `retrieve`.* Letting every question embed before checking whether the
tenant has anything published failed **four tests of 57**, across two files: the guard's own
`test_an_organization_with_no_published_chunks_makes_no_embedding_call` and
`test_the_prompt_is_the_ticket_alone_when_there_is_nothing_to_retrieve` in
`tests/integration/test_knowledge_draft_grounding.py`, and
`test_a_question_with_nothing_published_gets_the_specifications_sentence` and
`test_a_manager_and_an_agent_may_list_and_ask_but_not_add_or_delete` in `tests/api/test_knowledge.py`.
The failure mode is the one worth naming: in the suite `EMBEDDING_PROVIDER=openai` with no key, so the
unexpected call does not merely cost money — it fails, the log line is
`ai_call_failed ... reason='EMBEDDING_API_KEY is not configured'`, and two tests that were asserting a
refusal now assert a `503`. **An `EXISTS` question that leaks a provider call turns a refusal into an
outage**, which is a stronger argument for Decision 7 than cost alone.

Each break was reverted, and the reverts confirmed by re-running the five knowledge files green —
**70 tests, 0 failures**.

**The live end-to-end did not run, and this is why.** `.env` holds a Groq key and no embedding key:
`EMBEDDING_API_KEY` is absent, and neither Anthropic nor Groq publishes an embedding model, so there was
no vendor to make a real vector with. `scripts/phase_x_walkthrough.py` was still exercised — it checks
the key before it opens a socket — and it printed its own refusal and exited `0`:

    EMBEDDING_API_KEY is not set, so there is no live retrieval to walk through.

**`EMBEDDING_PROVIDER=fake` was available and is deliberately not what this is.** It would have kept the
upload, the object storage, the worker, the chunker, the pgvector column, the ledger, and every route
real — and the *ranking* would still have been a hash of the text, so an answer that came out right
would have come out right by luck. The suite is where that configuration belongs, because there the
vectors are scripted and the ranking is a decision; here it would be a demonstration of nothing. So
this document claims no live RAG run, and the retrieval properties above rest on the tests that assert
them with chosen vectors rather than on a transcript.

**The PDF fixture is asserted where it can be.** Both the unit tests and the walkthrough build a PDF by
hand rather than committing a binary, for the reason `app/core/file_validation.py` gives about signature
tables: one page of extractable text is what is needed, and a checked-in PDF is a file nobody can review,
diff, or explain. `tests/unit/test_document_text.py` reads its own fixture back through `pypdf` and is
part of the 1584; the walkthrough asserts the same thing before it uploads, because a hand-built PDF
that one reader opens and another does not is the sort of fixture that fails at the pipeline rather than
at the assertion — so `201` alone would not be evidence that the extraction had anything to extract.


