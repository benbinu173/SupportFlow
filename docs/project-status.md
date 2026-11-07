# Project checkpoint

Updated: 2026-11-07.

## Current milestone

F18 is committed as `a27616e` (frontend browser workflows and CI). Live backend
browser verification is implemented separately with `npm run test:e2e:live`.
Route code splitting is complete and verified.
The live checks, route code splitting, and this checkpoint are recorded together
in Git.
The backend's A–Z milestones are recorded as complete in the main README.
Local production deployment verification, the Redis worker dependency fix,
and the production-image CI check are committed as `6eb4bb3`.
Patched frontend dependencies and browser coverage through Caddy are now
implemented and verified as `b999bb5`.
Scheduler-driven SLA alerts are now verified end to end on the local production
stack through Caddy, with naturally aging tickets and captured email.

## F18 scope

- Playwright checks the production bundle with isolated HTTP and WebSocket
  fixtures: role navigation, ticket actions, public replies and private notes,
  AI draft review, failed-reply recovery, sessions, and realtime reconnects.
- Browser fixtures respect agent assignment and customer ownership. Reopening
  clears the assignment: agents return to the queue and their ticket cache is
  removed; customers keep access to their own reopened requests.
- The account menu opens `/preferences`. Developer diagnostic routes stay out
  of the production bundle. Existing component tests match the current screens.
- CI runs browser workflows and uploads failure reports, screenshots, and traces.

## Verification

- Lint and the realtime contract check passed.
- TypeScript checking passed.
- Vitest: 103 component/service cases verified across 15 files. Route/auth
  cases were rerun after adapting the tests to asynchronous screen imports.
- Playwright: 16 browser workflows passed, including protected-screen
  downloads, delayed loading, and failed-download recovery.
- Live backend: 2 browser workflows passed, including a repeat run against the
  retained test tenant.
- Production build passed without the large-chunk warning.

## Route code splitting

Screen modules and the auth/marketing layouts download on demand. The session
and permission guards remain eager. The role-home map is separate from sidebar
navigation so startup does not import the sidebar's icons. A Suspense boundary
announces `Loading page`; rejected downloads reach the existing route error
page, and a full reload retries them.

Production JavaScript sizes (decimal kB, before compression):

| Download | Before | After |
|---|---:|---:|
| Startup entry and all static dependencies | 1,344.1 kB | 603.8 kB |
| First marketing page, including its route and layout | 1,344.1 kB | 641.7 kB |
| First sign-in page, including its route and layout | 1,344.1 kB | 635.0 kB |
| Largest individual JavaScript chunk | 1,344.1 kB | 473.1 kB |

The build manifest contains 19 dynamic entries (17 screens and two layouts).
Analytics and its chart dependencies are deferred in their own 403.6 kB chunk.
These are JavaScript download measurements; they do not claim a measured
page-load timing improvement. CSS and font assets are outside this comparison.

## Live backend verification

Two browser workflows exercise the production frontend against a running
FastAPI, PostgreSQL, Redis, Celery worker, and Mailpit, using separate role
sessions and real HTTP and WebSockets:

- Manager assignment, agent replies and internal notes, customer replies,
  resolution, and customer close/reopen.
- Realtime assignment notifications, replies, and ticket status changes on the
  recipient's screen without a reload; internal notes stay private.
- Assignment, customer-reply, and resolution emails delivered to Mailpit,
  matched to their recipient and ticket reference.
- Agent reopen returns to the queue; reload and direct navigation verify that
  the unassigned ticket is no longer accessible to the original agent.

An independent PostgreSQL read confirmed the reopened ticket, cleared
assignment, retained first-response timestamp, two public replies, one private
note, and three notification rows with `emailed_at` set.

The live check reuses a dedicated local test tenant and retains generated
tickets for inspection. Instructions are in the frontend README. AI-provider
calls, scheduler-driven SLA alerts, and the deployed Caddy/TLS path are separate
checks; the existing walkthrough and smoke scripts remain under `backend/scripts/`.

## Local production deployment verification

Rebuilt and started `docker-compose.prod.yml` against the retained local
production volumes. The existing Acme Support and smoke tenants were reused;
the seed was not reset. Only Caddy publishes host ports.

- The existing deployment smoke script passed **23 checks, 0 failures**.
  The final run enabled TLS verification using the stack's public Caddy root
  certificate through `SSL_CERT_FILE`; HTTPS and WSS both validated successfully.
  This verifies the local CA and hostname, not public certificate trust.
- Liveness, PostgreSQL/Redis readiness, administrator login, API security
  headers and request IDs, two-sided tenant isolation, indistinguishable 404
  bodies, and a real `ticket.created` WebSocket event passed through Caddy.
- Separate HTTP checks verified the HTTP-to-HTTPS redirect, six frontend deep
  links, document CSP/security headers and `no-cache`, all 60 manifest assets
  with immutable caching (including 19 dynamic entries), and a missing-asset
  404. These checked HTTP responses, not browser interaction or CSP execution.
- The knowledge question returned a refusal with **zero sources**. Generation
  and embedding keys are empty, so this does not verify live AI or RAG. SMTP
  is also unconfigured in this local production environment.

The initial rebuild exposed a runtime-only dependency failure: Kombu 5.7.0's
Redis transport imports `packaging`, which is installed by its Redis extra.
Declaring plain `celery` plus the Redis client did not install that extra;
both worker and beat restarted with `ModuleNotFoundError: packaging` while
the HTTP/WebSocket smoke script still passed. The dependency is now
`celery[redis]==5.6.3` so transport requirements reach the runtime image.

CI now loads the built production image and initializes Celery's Redis broker
transport and result backend without development dependencies or a live broker.
That check reproduced the failure before the fix and passed on the rebuilt
image afterwards. The deployed worker responded `pong`, and beat started its
persistent scheduler. The README documents certificate verification from
PowerShell without modifying the host's trust store.

## Dependency updates and production browser workflows

The six reported dependency entries are resolved. The lockfile now installs
Vite 8.3.6, Vitest and coverage-v8 5.1.0, follow-redirects 1.16.2, and undici
7.30.2. `npm audit` and the Docker build's `npm ci` report **0 vulnerabilities**.
Vite's declared minimum was raised to the patched version; Vitest and coverage
were updated together to keep their peer versions aligned.

`npm run test:e2e:production` runs against `https://localhost` without Vite.
Setup/readback, browser HTTP, and WSS all use Caddy. Shared live-test helpers
retain the separate development suite's existing API/preview URLs. The
production suite reuses a dedicated **Local production browser checks** tenant.
The local `docker-compose.browser-checks.yml` override captures notifications
in Mailpit and exposes inspection only on `127.0.0.1:8026`.

The first browser run exposed the theme bootstrap being blocked by production
CSP. The inline script is now a classic, blocking same-origin file at
`/theme-init.js`, which nginx revalidates with `no-cache`. It still runs before
the body paints; `script-src 'self'` remains enforced. Browser reports/traces
are excluded from the Docker build context.

Verification after the changes:

- Lint, TypeScript, realtime contract, and Docker production build passed.
- All 103 component/service cases were verified across 15 files. The initial
  run passed 93 cases but hit a Windows worker-start timeout on the router
  file; that file's 10 cases passed on a targeted rerun.
- All 16 existing deterministic browser workflows passed.
- All 4 production browser checks passed: saved theme before application
  modules can load; admin Secure/HttpOnly/SameSite cookie rotation, lazy
  preferences loading, CSP, deep-link restoration and sign-out; manager/agent/
  customer conversations, privacy, lifecycle, realtime and captured emails;
  and agent access revocation after reopening clears assignment.

Chromium accepts the local certificate for browser interaction; cryptographic
certificate/hostname verification remains in the separate smoke check using
the stack's public CA. The normal production compose configuration is restored
after the browser checks, with the temporary Mailpit container removed and all
database/object-store volumes retained.

## Scheduler-driven SLA verification

`backend/scripts/sla_scheduler_check.py` passed **47 checks, 0 failures** on
2026-11-07, from 04:49:34 to 04:52:02 UTC. It observes the actual production
beat, Redis broker, Celery worker, PostgreSQL, HTTPS/WSS through Caddy, and
SMTP delivery to Mailpit. HTTPS/WSS certificate and hostname verification
use Caddy's public local CA certificate.

Only the dedicated **Local SLA Scheduler Checks** tenant received shortened
policies: first response in one minute, resolution in two minutes, warning
at 50%. Tickets aged naturally; no timestamps were rewritten and no tasks
were manually dispatched. The local override reduced beat's interval to
five seconds. This verifies delivery using an accelerated schedule, rather
than measuring alert latency under the normal five-minute interval.

- Beat dispatched automatically **29 times**, with gaps of 5.000–5.006 seconds.
- Assigned and unassigned tickets each received both warning and breach for
  both timers: **8 system-authored timeline events**, with the original
  deadlines preserved. Events were recorded **2.462–2.673 seconds** after
  their thresholds; these are event-recording delays, not email timings.
- The assigned agent received 4 alerts, and each of two managers received 8.
  **20 notification rows** matched both API inboxes and WSS frames. The own
  administrator and another tenant's administrator received none.
- Mailpit captured **20 matching emails**, one per SLA notification, with
  correct recipients, subjects, and ticket references; every row had
  `emailed_at` set. Worker logs independently confirmed four warnings, four
  breaches, 20 queued emails, and 20 published notifications.
- An internal note left the response timer running. On-time public reply and
  resolution stayed met without alerts; the inactive policy produced none.
  Another tenant's ticket read returned 404.
- Two further automatic tenant sweeps completed without duplicating events,
  notifications, or emails. All five WSS observers remained connected.

The checker handles Celery's nested JSON logs and uses IPv4 loopback for
local WSS transport while verifying the `localhost` certificate. Initial
attempts exposed a checker log-parsing mismatch and intermittent local WSS
connection timeouts; the final run passed with both addressed in the checker.
Ruff lint/format and strict mypy passed for the new script.

Original test-tenant policies are restored and all generated tickets are
closed; accounts and history remain for inspection. Normal production
compose settings are restored, including the 300-second scheduler interval
and original SMTP configuration. Mailpit is removed and storage volumes
are retained. This verifies captured SMTP delivery, not an external email
provider. The repeat procedure is in the main README; detailed evidence
is saved locally in the gitignored `backend/.sla-scheduler-report.local`.

## Next recommended step

Verify live AI analysis and knowledge ingestion/retrieval end to end. This
needs generation and embedding provider credentials; neither is configured
in the current local production environment.
