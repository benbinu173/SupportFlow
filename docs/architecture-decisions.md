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

