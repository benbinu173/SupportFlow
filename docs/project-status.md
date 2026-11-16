# Project checkpoint

Updated: 2026-11-16.

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
Live Groq generation is verified. The embedding credential is configured locally;
live RAG is blocked by exhausted OpenAI API credits.
Knowledge usage analytics is implemented and verified locally: organization totals include
document embeddings and knowledge questions, with refreshes after committed usage.
Password recovery is implemented and verified locally, with expiring workspace-specific email links,
password confirmation, audit entries, and revocation of the account's earlier sessions.
Password recovery and embedding preflight are committed as `fe8edbb`.
Email verification is implemented for newly registered and administrator-created accounts,
with explicit confirmation, generic resend, and sign-in restricted until confirmation.
The updated authentication release is now verified on the local production stack through
Caddy, including the actual schema upgrade, existing-user access, and delivered email links.
Release decision on 2026-11-12: proceed with public-deployment preparation for v1 and defer
live knowledge-base/RAG verification and release to v2. Further embedding checks are on hold.
Deployment constraint added on 2026-11-12: strict zero hosting budget and no payment card.
The earlier paid managed-service proposal is superseded; only card-free free plans are in scope.
The owner selected one Render origin for the frontend, API, and WebSockets on 2026-11-13.
The free preview profile is implemented and verified locally under its 512 MiB limit.
The Free Render service is live at
[supportflow-demo-9xgg.onrender.com](https://supportflow-demo-9xgg.onrender.com), with
the frontend, API, and WebSockets on one origin. Neon Free Postgres and Render Free
Key Value are ready in Singapore. Launch migrations are applied, verified Neon TLS
passes, and the private Supabase bucket passes the actual application's S3 adapter
checks. Public HTTPS, readiness, security headers, assets, anonymous access controls,
CORS, desktop/mobile auth screens, and disabled knowledge checks pass.
Brevo Free passes TLS authentication and worker submission using its documented
temporary sender replacement. The owner confirmed actual inbox delivery, explicit
email verification, and successful public sign-in on 2026-11-16. A read-only launch
database check confirms the retained registration is verified. The signed-in
dashboard reports `Live`, confirming its public WebSocket authenticated. All 14
automated public workflow checks now pass, including sessions, role/privacy,
ticket lifecycle, realtime recovery, and actual Supabase file downloads. Public
Groq generation and automatic SLA warnings/breaches also pass. Password recovery,
SLA inbox confirmation, and idle sleep/wake are the remaining public checks.

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

The build manifest at that checkpoint contained 19 dynamic entries (17 screens and two layouts).
Password recovery adds two more lazy screen modules.
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

## Live AI generation verification

`backend/scripts/live_ai_check.py --generation-only` passed **31 checks,
0 failures** on 2026-11-08. Docker Desktop was restarted and the retained
local production stack brought up. The checker used the existing Groq
credential from `.env` temporarily, with `openai/gpt-oss-120b`, and accessed
the API through Caddy with verified HTTPS.

- Classification and sentiment completed in the production Celery worker;
  validated results matched the ticket, whose actual priority stayed low.
- Summaries completed, remained separate from ticket classification/lifecycle
  fields, reused the same stored row on an unchanged conversation, and
  regenerated after a new message.
- Drafts and internal notes stayed private to staff. Regeneration retained
  both internal drafts. A human accepted an edited reply; the customer saw
  that public message, and acceptance made no additional model call.
- Regeneration and edited acceptance were audited. Another tenant received
  404 from the analysis, summary, and suggested-response routes.
- A read-only PostgreSQL witness confirmed **6 successful real calls** and
  **1 cache hit**, with 4,552 prompt tokens and 514 completion tokens. Live
  provider latencies were 636–879 ms. The ledger recorded **$0.000992** at
  published model prices; this is not a claim about the account's invoice.
  The cache hit had zero tokens and zero cost.

The first checker run compared the full ticket representation, including
the ticking SLA display, when asserting a summary changed no ticket fields.
That comparison was corrected to classification and lifecycle fields. The
final run passed; no application behavior needed changing. Ruff lint/format
and strict mypy passed for the checker.

The generated tickets are closed. Original production AI/embedding settings
are restored and `.env.ai-checks.local` is removed. Provider credentials were
never printed or committed; the original `.env` files were preserved. The
repeat procedure is in the main README, and local evidence is saved in the
gitignored `backend/.live-ai-generation-report.local`.

## Live RAG attempt and quota handling

The embedding credential is now stored in the gitignored `.env`, with
`EMBEDDING_PROVIDER=openai` and `EMBEDDING_MODEL=text-embedding-3-small`.
The full checker ran on 2026-11-08 from 04:34:10 to 04:35:07 UTC and recorded
**32 passed, 2 failed**. It stopped when the worker could not ingest the first
knowledge document. A minimal direct embedding request confirmed HTTP 429,
type `insufficient_quota`, and code `credit_balance_exhausted`; no vector was
returned. This is an API credit blocker, and no further live embedding calls
are being made until credits are available.

A replacement embedding credential was saved locally and checked with one
minimal request on 2026-11-08. It returned the same HTTP 429 and
`credit_balance_exhausted`, with no embedding vector. The full checker was
not rerun because embedding access remains unavailable. Sanitized probe
evidence is in the gitignored `backend/.embedding-quota-probe.local`.

The attempt exposed two application issues that are now fixed:

- Both OpenAI quota error codes are treated as permanent failures, preventing
  retries that cannot succeed without credits. Ordinary rate limits remain
  eligible for retry.
- Knowledge search commits its provider usage after a successful answer and
  after an AI failure, so usage records survive the request's database session.

The checker now captures scoped database evidence and document processing
states before cleanup on failed runs. Provider and knowledge regression suites
passed **83 tests**, including separate-connection checks of committed usage
after successful answers, embedding failures, and generation failures. Ruff
lint/format and strict mypy passed, and the production backend image rebuilt
successfully and was applied to the local stack.

Normal production provider settings are restored, the temporary credential
file is removed, generated knowledge documents are deleted, and the generated
ticket is closed. The failed report remains in the gitignored
`backend/.live-ai-rag-report.local`; the earlier generation pass remains valid.
Credentials are excluded from reports and Git.

## Knowledge usage analytics

The organization overview now includes AI calls made without a ticket, including
document ingestion and knowledge questions. Calls, failures, tokens, cost, and the
operation breakdown use the existing ledger's tenant boundary. Agents' analytics
remain scoped to their assigned tickets.

Searches invalidate the Redis analytics cache after committing usage on successful
answers, refusals after embedding a query, and provider failures. Document processing
does the same on completion or failure, with a Redis client scoped to each worker
invocation. The browser invalidates its analytics queries when a question settles
or observed document processing finishes. The embedding operation is labeled
**Knowledge indexing and search** to reflect both uses.

The backend operator scripts now have an explicit package marker so the normal
whole-project mypy command resolves shared checker imports consistently. Generated
frontend API declarations reflect the updated usage scope documentation.

Live embedding calls remain deferred until API credits are available. This work
uses scripted test providers against the local test database and deterministic
browser fixtures.

Verification passed:

- 174 backend cases across analytics caching, knowledge APIs, usage persistence,
  ingestion, retrieval, draft grounding, and tenant/role isolation.
- All 103 frontend component/service cases across 15 files.
- All 20 production-bundle browser workflows, including four new knowledge
  analytics checks for successful and failed questions and document processing.
- Backend Ruff lint/format and strict mypy (145 source files); frontend lint,
  TypeScript, and the realtime contract check.

## Embedding preflight and resumed checkpoint

A single minimal embedding request on 2026-11-10 at 10:34 IST still returned HTTP 429,
`insufficient_quota` / `credit_balance_exhausted`, with no vector. Sanitized evidence
is saved in the gitignored `backend/.embedding-access-probe.local`. Existing credentials,
environment files, and full-run reports were preserved; the full RAG checker was not rerun.

`scripts.live_ai_check --preflight-only` now checks embedding access without Docker or a
generation credential. Full checks run the same preflight before preparing the stack and
stop on unavailable access or a vector incompatible with the database. Blocked preflight
reports are separate from the full RAG report. A stopped Docker daemon also fails before
a temporary credential file is created. The direct preflight request consumes provider
quota outside the application's usage ledger.

The preflight and existing embedding-provider regression suites passed **50 tests**.
Strict mypy passed for `app` and `scripts` (140 source files). No further live embedding
calls were made after the credit failure.

## Password recovery

The sign-in screen links to `/forgot-password`. Known, unknown, disabled, and
queue-failure requests receive the same public confirmation. Up to five active accounts
sharing an email across active workspaces receive individual workspace-named messages
through the existing notifications worker. Links expire after 30 minutes by default;
request and confirmation endpoints have separate rate limits, including a hashed-email
request bucket.

Emails use a validated `PUBLIC_APP_URL` and put the recovery credential in a fragment.
The reset page removes it from the address and retains it only in component memory.
It checks the shared password policy and matching confirmation, handles missing/expired/
used links, and clears the current browser session and private query cache after success.
Recovery pages remain available while signed in. Desktop and phone layouts use the
existing auth design, with room between the recovery panel's prose cards.

Reset locks the account row, increments its credential version, revokes refresh rows,
and records a safe audit entry in one transaction. Old access tokens fail on their next
request; WebSockets receive a local close signal and periodically check fresh database
state across API instances. The worker generates the signed link, so no usable recovery
credential is stored or queued. Login rehashing is serialized with reset. Refresh reuse
is limited to the presented password version, protecting new-password sessions from
old-device retries and pre-reset cookies, including a late old-version refresh row.

Migration `d64f197a3c20` adds account/refresh versions and the password-reset audit action.
It is applied to the retained local development database; `alembic check` reports no
schema differences. Production compose carries the public origin to API and worker;
the migration service applies the new revision on deployment. Production SMTP and the
actual public frontend origin must be configured for delivered links.

Verification completed:

- The complete backend suite passed all 1,675 cases.
- Frontend: all 113 component/service cases passed across 16 files; the final auth and
  route changes also passed 27 targeted cases.
- All 24 production-bundle browser workflows passed, including four recovery cases.
- Frontend lint, TypeScript, realtime contract, and production build passed.
- Backend Ruff lint/format and strict mypy passed (147 source files).
- An independent real-HTTP regression confirmed that a pre-reset cookie returns 401
  while the new-password session still refreshes with 200 after that retry.
- The live Mailpit recovery browser case passed against the updated API and worker,
  including an old-cookie retry after a fresh sign-in. Final desktop/mobile screenshots
  were inspected and the verification record is saved in the local Playwright report.

The live browser checker uses a unique agent in the dedicated development tenant and
follows its unaltered Mailpit link. It verifies session revocation, replay refusal,
sign-in/reload with the new password, and old-cookie retries after that sign-in. It
deactivates only its generated account and retains mailbox/history, desktop/mobile
screenshots, and a sanitized record. Credential-bearing traces are disabled.
The repeat procedure is in the frontend README; [ADR-035](architecture-decisions.md#adr-035--password-recovery-uses-account-versions-and-workspace-specific-email-links)
records the security and concurrency choices.

Temporary verification API, worker, and preview processes are stopped. Development
infrastructure and test history are retained; the real environment files were preserved.

## Email verification

Registration now returns `201 {email, detail}` without session credentials or a refresh
cookie. Newly registered administrators and users created through `/users` receive
workspace-named verification mail through the existing notifications worker. Unverified
accounts cannot log in, refresh, authenticate protected HTTP requests, or open an
authenticated WebSocket. Correct credentials receive `403 EMAIL_NOT_VERIFIED` and the
sign-in screen offers a verification request.

Public resend returns the same `202` confirmation for known, unknown, verified, disabled,
and queue-failure cases. It sends at most five links across active workspaces, with
separate address/email limits and a separate confirmation limit. The worker signs a
purpose-separated credential bound to the user, organization, current email fingerprint,
and original expiry; no usable credential is queued. Links expire after 60 minutes by
default and use the validated public frontend origin.

The browser opens `/verify-email`, removes the fragment, and confirms only after a click.
It supports waiting, resend, invalid/expired/used links, success, and phone layouts. It
captures a link arriving on the same waiting route and ignores responses for older links.
Confirmation locks the account row, stamps `email_verified_at`, and audits `email_verified`;
concurrent or sibling links cannot succeed twice. It does not start a session. Continue
to sign in explicitly switches any existing browser account and supplies the confirmed
email and workspace slug, avoiding cross-workspace ambiguity in that flow.

Migration `e73a61b24f09` is applied to the retained development database. Existing users
are backfilled to preserve access; this baseline does not claim a historical email
confirmation. New users have no default verification. Example environments document the
expiry and rate limits. Local walkthroughs and deployment/live checkers now
complete generated accounts through Mailpit and require the notifications worker. Real
environment files and infrastructure are retained.
The demo seeder marks only its canned tenant's accounts verified as an explicit local
trust exception; public registration and administrator-created accounts still require mail.

[ADR-036](architecture-decisions.md#adr-036--new-accounts-confirm-email-before-a-session-is-issued)
records the account, link, migration, and workspace-selection decisions.

Verification completed:

- All 125 cases in the affected audit/user API, tenant/log-security, and mailbox-helper
  suites passed. The full 1,712-case backend run passed 1,700 and exposed four outdated
  expectations plus eight fixture errors, all corrected and covered by this focused run.
  Verification API/security, worker delivery, concurrency, and migration compatibility
  passed in the full run. The complete suite was not repeated after the test-only fixes.
- All 123 frontend component/service cases passed across 17 files in a complete run.
- All 28 deterministic production-bundle browser workflows passed, including four
  verification workflows and the existing recovery, role, knowledge, and session checks.
- Four live browser cases passed against the real development API and Mailpit:
  verification, password recovery, the manager/agent/customer workflow, and agent reopen.
- Backend Ruff lint/format and strict mypy passed (150 source files). Frontend lint,
  TypeScript, the realtime contract check, and the production build passed.
- The retained development database is at `e73a61b24f09`; `alembic check` reports no drift.

Desktop and phone verification screenshots were inspected. Sanitized verification
records, screenshots, Mailpit messages, and test history are retained. Temporary API,
worker, and preview processes are stopped; the real environment files were preserved.

## Production authentication release verification

On 2026-11-12, rebuilt and started the production images with the local Mailpit override.
All application HTTP and browser traffic used `https://localhost` through Caddy; realtime
used WSS. Existing production volumes and the real environment files were preserved.

The retained production database started at `51815e0bbd16`. The one-shot migration service
applied password recovery (`d64f197a3c20`) and email verification (`e73a61b24f09`) before the
API and worker started. Independent PostgreSQL snapshots confirmed that all **18 existing
users, 7 organizations, and 72 tickets** remained present, with user identity, role, active
state, and customer links preserved. Existing users received the compatibility verification
baseline; new accounts still have no verification default. Both authentication audit labels
are present. `alembic check` reported no schema drift.

Verification completed:

- **All 7 production browser workflows passed**, using the deployed frontend and real API,
  PostgreSQL, Redis, Celery worker, Caddy, and SMTP delivery to Mailpit.
- A new-workspace signup returned `201 {email, detail}` with no session credentials or
  refresh cookie. Its original delivered email named the new workspace and linked to
  `https://localhost/verify-email`. Opening the link left sign-in blocked; clicking to
  confirm enabled workspace-specific sign-in, reload, and WSS. Sign-out cleared the cookie,
  and reopening the used link showed the request-new-link screen.
- An administrator-created account was blocked before confirmation, received its requested
  verification mail, confirmed explicitly on the deployed phone layout, rejected replay,
  and signed in successfully after verification.
- Password recovery followed its unaltered delivered HTTPS link. Reset logged out the other
  browser, rejected old access tokens, refresh cookies, the old password, and reused links.
  New-password sign-in and reload remained valid even after a retry with an old cookie.
- The existing administrator, role workflows, customer privacy, ticket lifecycle,
  realtime updates, and notification emails also passed. Production cookie flags,
  rotation, CSP, saved theme bootstrap, lazy screens, deep links, and sign-out were checked.
- **23 deployment smoke checks passed with TLS verification enabled**, including HTTPS and
  WSS certificate/hostname validation using Caddy's public local CA, existing demo-account
  access, readiness, security headers, and two-sided tenant isolation.
- PostgreSQL readback found six email-verification and two password-reset audit entries
  across the initial and final runs. The generated recovery accounts advanced to credential
  version 1, with all earlier-version refresh rows revoked. Generated recovery and
  administrator-created verification accounts were deactivated by the checks.
- Frontend lint and TypeScript checking passed. The Docker frontend production build passed.

The first browser run exceeded the normal ten-login-per-minute limit as authentication
coverage expanded. The live helpers now honor the real `429 Retry-After` response before
retrying API and browser sign-in; authentication cases allow time for those windows.
The final run passed under the original production limits. No application authentication
behavior or production rate-limit settings were changed.

Desktop and phone screenshots were inspected. Sanitized Playwright records and screenshots
are retained under `frontend/playwright-report/production` and
`frontend/test-results/production`. The database/environment verification record is saved in
the gitignored `backend/.production-auth-report.local`, with the pre-upgrade snapshot in
`backend/.production-auth-baseline.local`. Two uniquely named signup workspaces and generated
test history remain available for inspection.

The normal production compose configuration was restored, the temporary Mailpit container
removed, and the updated services left running. The repeated migration completed with no
new upgrade to apply. This verifies captured SMTP delivery and trust in the local Caddy CA;
the actual public frontend origin and external SMTP provider still need deployment setup.

## Free v1 preview verification

`render.yaml` and `docker/Dockerfile.preview` prepare one Free Render web service for
the frontend, loopback API, a single prefork worker slot, and beat. nginx routes HTTP
and WebSockets on one origin, retaining CSP, the external theme bootstrap, and SPA
deep links. Startup serializes migrations with a PostgreSQL advisory lock; a Redis
lease prevents overlapping preview schedulers from dispatching concurrently.

Knowledge navigation, its route, and its screen chunk are omitted from v1 builds.
Backend knowledge endpoints return `503` before data/provider access by default.
The retained implementation requires both `KNOWLEDGE_ENABLED=true` and frontend
`VITE_KNOWLEDGE_ENABLED=true` to restore it for v2 after live verification.

Verification completed on 2026-11-13:

- All **7 live preview browser workflows passed** against the isolated image, real
  PostgreSQL/Redis, its bundled prefork worker, Caddy HTTPS/WSS, and Mailpit. These
  cover signup/verification/replay, password reset and earlier-session revocation,
  secure cookies and rotation, CSP/theme/lazy screens, role privacy, realtime, and
  ticket lifecycle. Preview history and storage are separate from production.
- Container memory peaked at **467.4 MiB under a hard 512 MiB limit** during this
  workload, with zero OOM events or kills. This is measured demo verification,
  not a capacity guarantee for concurrent public signups or real AI-provider calls.
- **11 local lifecycle/memory checks passed**: a second scheduler waited without
  taking the active lease, the owner renewed it, lease loss stopped the original
  scheduler and its supervised container, the replacement took over, graceful
  shutdown released the lease, and the preview restarted healthy with a new lease.
- All **123 frontend component/service tests** passed across 17 files. Frontend
  lint, TypeScript checking, and the realtime contract check passed.
- The deterministic production-bundle browser suite passed **25 cases**, including
  hidden knowledge navigation and direct-URL refusal; the four deferred knowledge
  cases were skipped explicitly.
- All **46 backend configuration/knowledge-release cases** passed. Ruff lint/format
  and strict mypy passed for application and operator scripts (146 source files).

The live helpers use already-delivered creation emails for new setup accounts;
only the explicit resend workflow and recovery of retained pending accounts request
fresh links. Login helpers honor the normal `429 Retry-After` policy. Verification
and recovery traces stay disabled to avoid recording credentials.

Browser reports/screenshots remain in gitignored `frontend/playwright-report/preview`
and `frontend/test-results/preview`. Sanitized lifecycle/memory evidence is in
`backend/.preview-report.local`, with the repeat checker in
`backend/.preview-check.local.py`. When running, the isolated preview is reachable at
`https://localhost:8443`; production services, real environment files, and retained
volumes are preserved.

## Public free v1 deployment

Reuse the provisioned resources in the owner's confirmed Render **My Workspace**
(`tea-cundai9opnds73d530i0`):

- Web service `srv-dbtdqerbc2fs73dsidl0`: Free, Singapore,
  `docker/Dockerfile.preview`, `/health/ready`, automatic deployments off.
  The assigned origin is `https://supportflow-demo-9xgg.onrender.com`.
- Neon **supportflow-demo**, project `cold-term-95520688`: Free PostgreSQL 17,
  Singapore, production branch `br-icy-leaf-a1rznszb`, database **supportflow**.
  Launch revision `e73a61b24f09` and all 16 public tables are installed: 15
  application tables plus Alembic metadata.
  Client TLS uses `sslmode=verify-full&sslrootcert=system`; `vector` 0.8.7 and
  `pg_trgm` 1.6 are available. The migration-check branch
  `br-shiny-base-a1g0y1q3` remains separate from launch and retained local data.
- Render **supportflow-demo-redis**, `red-dbra2ctbedkc73d0g5k0`: Free, Singapore,
  `noeviction`, persistence off. Cache uses database 0, Celery broker/results 2.
- Supabase Free: the owner created a private **supportflow-attachments** bucket.
  The preview image's actual adapter passed upload, download, streaming, anonymous
  access refusal, and probe deletion. Public browser upload/download, exact byte
  comparisons, internal-file privacy, and tenant isolation now pass. Supabase's
  MCP connection remains read-only; setup was
  completed in the Dashboard without changing its access grant.

The first public container crashed because worker task modules selected the absent
`psycopg2` driver from a driverless PostgreSQL URL. Commit `d3886e5` uses the existing
psycopg SQLAlchemy DSN across worker modules and adds an image-level import check to
CI. All 773 unit cases, lint, formatting, and mypy passed; its
[full CI run](https://github.com/benbinu173/SupportFlow/actions/runs/43855045423)
passed. Commit `328d352` first corrected Render's private ingress hops, with
[all CI jobs passing](https://github.com/benbinu173/SupportFlow/actions/runs/43859225140).

Public forwarding probes then showed Cloudflare's public proxy addresses still
being selected as clients. Commit `4159045` also trusts Cloudflare's published
IPv4/IPv6 ranges. Eight regression cases failed before that correction; all 20
HTTP/WebSocket cases and seven actual nginx forwarding checks now pass locally,
along with Ruff, formatting, mypy, and the rebuilt image. Its public rollout and
forwarding recheck completed on 2026-11-16: deployment
`dep-dbtf0ajncjis73a321pg` is live, and three alternating normal/forged request pairs
on one connection retain the same public client address, excluding Cloudflare and
all supplied forged addresses. Sanitized evidence is in gitignored
`backend/.public-forwarding-report.local`.

Public smoke verification on 2026-11-16 passed nine groups: trusted HTTPS health and
dependency readiness, HTTP redirect, SPA deep links/security headers/theme script,
hashed assets with immutable caching and no knowledge bundle, anonymous access
refusal and disabled knowledge endpoints, CORS, desktop auth/marketing screens,
mobile signup/theme/viewport, and absence of JavaScript/CSP/asset errors. Measured
public memory stayed below 386 MiB of 512 MiB during the earlier checks.
Sanitized evidence is in gitignored `frontend/.public-smoke-report.local`.

The public platform still drops some invalid-authentication WebSocket close frames.
Successful password-authenticated WSS works for all four roles. Commit `c98ba6e`
deployed a 15-second opening/authentication deadline, one session-refresh attempt,
timer cancellation, and protection against stale socket close events. All seven
targeted cases, three relevant browser cases, and full CI passed. The public
workflow dropped the first real server acknowledgement and verified successful
recovery through a second genuine server connection.

Public browser upload exposed a separate bug: a fixed JSON header caused Axios
to serialize FormData as `{"file":{}}`, and the API correctly returned `422`.
Commit `a89d5d0` lets Axios select the body type. Its multipart regression failed
before the fix; all 12 API-client/interceptor cases, lint, TypeScript checking,
and a production-bundle upload/download browser case passed afterward.
[Full CI passed](https://github.com/benbinu173/SupportFlow/actions/runs/43905152466).
Deployment `dep-dbtirirl550s73dddnu0` became live at 16:24 UTC on 2026-11-16,
serving `assets/index-Bk0Rqa1X.js` with healthy PostgreSQL and Redis checks.

The deployed application passed **14 public workflow groups** using five normally
verified test accounts in **SupportFlow Public Checks** and **SupportFlow Public
Isolation**. Checks include normal login, four-role browser WSS, Secure/HttpOnly/
SameSite cookie rotation, lazy screens and CSP, assignment notifications, two-way
replies, private notes/files, byte-exact Supabase upload/API/browser downloads,
anonymous and tenant access refusal, invalid uploads, resolve/close/reopen,
disabled knowledge, bounded realtime recovery, and sign-out. No verification
bypass was used. Sanitized evidence is in `frontend/.public-workflow-report.local`.

Public Groq calls through the normal AI endpoints completed classification,
sentiment, summary, and suggested reply with `openai/gpt-oss-120b`. PostgreSQL
readback confirms results, token usage, and the usage ledger. A second unchanged
summary was served from the stored result with a cached usage row. The draft
stayed private until the agent reviewed and edited it in the actual browser.
Customer AI operations were refused. No embedding or knowledge-answer usage rows
exist. Evidence is in `frontend/.public-ai-report.local` and
`backend/.public-ai-database-report.local`.

The automatic public scheduler recorded both warnings and both breaches on a
naturally aging test ticket. The manager received all four notifications without
a browser reload; manager and agent each have four persisted notification rows.
All eight have `emailed_at` set after successful SMTP submission. Only the test
workspace's urgent policy was shortened, then restored to its original values.
The test ticket is closed. Evidence is in `frontend/.public-sla-report.local` and
`backend/.public-sla-database-report.local`; actual inbox confirmation is pending.

A consistent, certificate-verified, read-only PostgreSQL snapshot was exported at
15:49 UTC and restored into an isolated temporary database. All 16 table counts
and revision `e73a61b24f09` matched. Stock PostgreSQL could not replay Neon's
`pg_attver` column options; the local SQL copy omitted only 175 matched option
statements, leaving COPY data unchanged. The original custom archive is retained
in `backend/.public-backups.local/launch-20261116T154913Z.dump`. The temporary
restore container was removed. Evidence is in `backend/.public-backup-report.local`.

The measured public workload stayed below Render's 512 MiB limit, peaking at
**499.0 MiB** during authentication, with CPU usage below 0.075 cores in the
sampled windows. Redis peaked at **3.6 MiB of 25 MiB**. These measurements cover
the controlled demo workload and leave little memory room at the authentication
peak. The source database is about 9.3 MiB; Neon account reads confirm the Free
plan. The three generated tickets are closed and their history/files retained.

## Next required work

Brevo setup is complete. The owner confirmed actual Gmail arrival, explicit
verification, successful sign-in, and a `Live` dashboard. Preserve the verified
sender, port 2525/STARTTLS, authorized Render outgoing ranges, JWT secret, and
other saved connections in `.env.render.local`.

Remaining public checks:

1. Complete recovery through the real emailed link for the isolated test
   administrator; verify refresh replay refusal, earlier-session revocation,
   new-password login, and harmless retries of pre-reset cookies.
2. Confirm actual SLA warning and breach inbox arrival. SMTP acceptance for all
   eight manager/agent notification rows is already verified.
3. Observe automatic idle sleep, then verify session, ticket, and file survival
   on wake-up. Close all browser tabs for the public service during this period.
   A redeploy alone does not establish idle sleep/wake.

Operator reports, generated check credentials, saved browser sessions, mailbox
links, and backup archives remain gitignored and excluded from Docker builds.
Render's idle sleep pauses its bundled worker and scheduler; free Redis can lose
queued work on restart. Continue using only card-free Free plans.

The deployment procedure is in [deployment-free-demo.md](deployment-free-demo.md).
The earlier [deployment-render-vercel.md](deployment-render-vercel.md) remains a paid
hosting reference. Retained local production data and volumes are preserved.

Live RAG remains deferred to v2 by the user's release decision. Keep knowledge
disabled and make no further embedding calls during v1 verification. Semantic
retrieval, live document embeddings, grounded answers/citations, and RAG-grounded
drafts remain **not yet verified**.
