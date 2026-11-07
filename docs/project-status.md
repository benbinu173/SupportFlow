# Project checkpoint

Updated: 2026-11-07.

## Current milestone

F18 is committed as `a27616e` (frontend browser workflows and CI). Live backend
browser verification is implemented separately with `npm run test:e2e:live`.
Route code splitting is complete and verified.
The live checks, route code splitting, and this checkpoint are recorded together
in Git.
The backend's A–Z milestones are recorded as complete in the main README.
Local production deployment verification is now complete. The Redis worker
dependency fix, production-image CI check, and updated runbook are in the
working tree following `52c90a4`.

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

## Next recommended step

Review and update the frontend dependencies flagged during the image build.
`npm audit --omit=dev` reported one high-severity entry in `follow-redirects`,
an Axios Node adapter dependency; the production frontend runs as static browser
assets behind nginx, so the report alone does not establish browser exposure.
The full install reported six entries (three moderate, three high). Package
updates have not been applied in this deployment verification step.

Then extend live browser workflows to the deployed HTTPS origin to verify
sign-in, secure refresh cookies, lazy screens, CSP, and realtime behavior in
the browser through Caddy. The current live suite uses the Vite preview proxy.
