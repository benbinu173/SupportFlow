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
