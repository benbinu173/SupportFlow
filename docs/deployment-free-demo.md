# Free demo deployment

Updated: 2026-11-16. The owner selected a single Render URL for the frontend, API,
and WebSockets. Hosting must cost zero and require no payment card. RAG is deferred
to v2. This is a small demo with free-plan availability limits.

The public service is live at
[supportflow-demo-9xgg.onrender.com](https://supportflow-demo-9xgg.onrender.com), Render
service `srv-dbtdqerbc2fs73dsidl0` in the owner's confirmed **My Workspace**
(`tea-cundai9opnds73d530i0`). Database, Redis, and private storage are provisioned.
The owner's registration is retained. On 2026-11-16, the owner confirmed receipt of
the Brevo verification email, explicit verification, and successful public sign-in.
A read-only launch database check confirms the account is verified. The signed-in
dashboard reports `Live`. All 14 automated public workflow groups now pass,
including secure sessions, ticket lifecycle, realtime recovery, role/tenant
privacy, and actual Supabase browser upload/download. Public Groq generation,
automatic SLA warnings/breaches, emailed password recovery, and an isolated
PostgreSQL backup restore also pass. The owner confirmed SLA inbox delivery.
Genuine automatic idle sleep and all 12 public wake checks also pass, completing
public v1 deployment verification.
Deployment `dep-dbtirirl550s73dddnu0` serves commit `a89d5d0` after
[successful CI](https://github.com/benbinu173/SupportFlow/actions/runs/43905152466).
See
[project-status.md](project-status.md).

## Prepared source

- `render.yaml` declares one **Free** Docker web service in Singapore. Automatic
  deployments are off. The current assigned hostname is
  `supportflow-demo-9xgg.onrender.com`; reuse this existing service.
- `docker/Dockerfile.preview` builds the React bundle and runs nginx, one Uvicorn
  process, one Celery prefork worker slot, and beat in that web container. The
  worker consumes notifications, SLA, AI, and maintenance. Hard task timeouts
  remain available. Knowledge work is deferred.
- The startup script runs Alembic before starting the application. A PostgreSQL
  advisory lock serializes migrations across overlapping replacement containers.
  A renewable Redis lease keeps one preview beat active; lease loss stops it.
  A failed child stops the container, and termination shuts down its children.
- nginx serves the SPA and proxies `/api/*`, `/health*`, and `/ws`. Render owns
  public TLS. Frontend requests use relative URLs, and email links use the exact
  `PUBLIC_APP_URL`. The existing Secure, HttpOnly, SameSite=Lax refresh policy fits
  this single origin. The API listens only on loopback inside the container.
  Uvicorn trusts loopback, Render's private ingress range, and Cloudflare's published
  proxy ranges, selecting the rightmost untrusted client from `X-Forwarded-For`.
  Review the published ranges if Render changes its ingress routing.
- CSP, the external theme bootstrap, deep-link fallback, document revalidation,
  and immutable hashed assets are retained. Root `.dockerignore` excludes local
  environments, credentials, virtual environments, and browser reports.
- v1 builds omit the knowledge route and its screen module, hide navigation, and
  replace the landing page's RAG claims. API knowledge operations return `503`
  before data access while `KNOWLEDGE_ENABLED=false`. Keep the existing schema for
  v2. Enabling both backend `KNOWLEDGE_ENABLED=true` and frontend build variable
  `VITE_KNOWLEDGE_ENABLED=true` restores the feature after live verification.

## Local verification completed

On 2026-11-13, all seven live browser workflows passed against the isolated preview:
signup, verification and replay refusal, recovery/session revocation, secure cookies,
CSP/theme/lazy screens, role privacy, ticket lifecycle, realtime, and captured email.
Container memory peaked at 467.4 MiB under its hard 512 MiB limit, with no OOM events.
Eleven lifecycle/memory checks also verified scheduler lease renewal, overlapping
replacement, lease-loss shutdown, takeover, graceful release, and healthy restart.
The local checker and sanitized report are gitignored at
`backend/.preview-check.local.py` and `backend/.preview-report.local`.

Frontend lint, TypeScript, realtime contract, all 123 component tests, and 25
deterministic browser workflows passed; four deferred knowledge cases were skipped.
The backend's 46 configuration/knowledge-release cases, Ruff, and strict mypy passed.
The built preview omits the knowledge screen, and its API refuses knowledge operations.
Those 2026-11-13 checks verify local dependencies and Mailpit. Separate public and
external-service results are recorded below and in `project-status.md`.

## External free services

| Component | Service | Configuration |
|---|---|---|
| PostgreSQL | Neon Free | PostgreSQL 17, direct TLS connection URL, `vector` and `pg_trgm` extensions |
| Redis | Render Free Key Value | Same Singapore region, internal URL, `noeviction`; database 0 for cache and 2 for Celery |
| Files | Supabase Free Storage | Private `supportflow-attachments` bucket; S3 endpoint, region, server-only S3 access keys |
| Email | Brevo Free | Verified sender, `smtp-relay.brevo.com:2525`, STARTTLS, SMTP Login/key; free-email senders use its temporary address replacement |
| AI generation | Existing Groq free access | `AI_PROVIDER=groq`, `AI_MODEL=openai/gpt-oss-120b`; use the existing private generation credential |

Neon and Redis are already provisioned and authenticated account reads verified:

- Neon **supportflow-demo**, project `cold-term-95520688`, Free, PostgreSQL 17,
  Singapore (`aws-ap-southeast-1`). Use the **production** branch
  (`br-icy-leaf-a1rznszb`) and database **supportflow** for launch. Its direct TLS
  URL is saved in `.env.render.local`. Launch migrations have installed revision
  `e73a61b24f09` and all 16 public tables (15 application tables plus Alembic
  metadata); the owner's registration is retained.
- The isolated **release-check-2026-11-13** branch (`br-shiny-base-a1g0y1q3`)
  passed startup migrations and `alembic check` at revision `e73a61b24f09`.
  The actual preview image verified the Neon certificate using the system CA
  bundle and `sslmode=verify-full&sslrootcert=system`. On 2026-11-16, a read-only
  image check also verified launch-database TLS and both available extensions.
- Render **supportflow-demo-redis**, `red-dbra2ctbedkc73d0g5k0`, is available in
  the owner's confirmed **My Workspace** (`tea-cundai9opnds73d530i0`), Free,
  Singapore, with `noeviction` and persistence off. Its exact internal URL and
  database 0/2 variants are saved privately in `.env.render.local`, with a copy
  in `.env.render.redis.local`.

The owner's Supabase Free project and private bucket are provisioned. On 2026-11-16,
the preview image's actual S3 adapter passed upload, download, streaming, anonymous
access refusal, and probe deletion. Sanitized evidence is in the gitignored
`backend/.storage-readiness-report.local`. Supabase setup was completed manually in
its Dashboard; its MCP connection remains read-only.

The SMTP2GO TLS/authentication probe passed on port 2525, but actual public sending
failed with `550 From header sender domain not verified`. A successful SMTP login
does not verify the From domain. The owner cannot manage the original domain's DNS,
and SMTP2GO also excludes Gmail single senders. Reuse the other existing resources
and Brevo Free; choose only card-free Free plans.

Brevo's exact SMTP Login and active key pass certificate-verified TLSv1.3
authentication. The verified Gmail sender and authorized Render outgoing ranges
are deployed with port 2525/STARTTLS. The SMTP-only update preserved the other
20 service environment values. The owner confirmed actual inbox arrival,
explicit verification, and public sign-in; a read-only launch check confirms the
registration is verified. Five additional check accounts were verified normally
through their emailed links. Eight automatic SLA notification rows have successful
SMTP submission timestamps, and the owner confirmed warning and breach emails
arrived for both manager and agent accounts. Public recovery through a fresh
emailed link passed all 16 checks, including earlier-session revocation, single-use
links, normal new-password login, and harmless old-cookie retries. A read-only
database witness confirms the password-reset audit and no live pre-reset sessions.
Sanitized evidence is in gitignored `backend/.public-email-current-report.local`
and `backend/.public-sla-database-report.local`, with recovery evidence in
`frontend/.public-recovery-report.local` and
`backend/.public-recovery-database-report.local`.

Automatic Render idle sleep was observed over 17.7 minutes with public-service tabs
and check browsers closed. A complete application-log audit confirms no user
requests or new WebSocket connections during that window. The API and worker shut
down cleanly without a deployment, manual suspension, or failure event. All 12 wake
checks passed: the saved login cookie restored and rotated, realtime returned to
`Live`, PostgreSQL/Redis were ready, and the closed ticket, messages, and Supabase
files survived. API and browser downloads matched the original bytes. Knowledge
remained disabled. Browser readiness took 80.5 seconds on this cold start. Evidence
is in gitignored `frontend/.public-idle-evidence.local`,
`frontend/.public-idle-traffic-report.local`, and `frontend/.public-wake-report.local`.

Render documents [512 MB and 0.1 CPU for free web services](https://render.com/docs/compute-plans).
It [sleeps after 15 idle minutes](https://render.com/docs/free), suspending the bundled
worker and scheduler too. SLA checks resume after wake-up; alerts can arrive late.
Its free Redis loses queues, cache, rate limits, results, and the scheduler lease on
restart. Database-backed tickets and sessions survive, but queued mail and AI work
are best effort. Users can request a fresh verification/reset link after queue loss.
There is no automatic durable outbox in this profile. Render may suspend services
or builds when free quotas are exhausted; keep payment methods absent.

[Neon Free](https://neon.com/docs/introduction/plans) has limited compute, data transfer,
and recovery. Check its current limits in the account before launch. Periodic SLA
queries and open sessions can keep database compute active; monitor compute hours.
Use a direct PostgreSQL URL with its supplied TLS parameters for the startup advisory
lock and migrations. Avoid a transaction-pooler URL for that session-level lock.
Render Free Postgres is unsuitable for this demo because it expires after 30 days.

[Supabase Free Storage](https://supabase.com/pricing) has storage/egress limits and can
pause an inactive free project. Its [S3 keys bypass storage RLS](https://supabase.com/docs/guides/storage/s3/authentication)
and must stay in backend secrets. Application tenant authorization still protects
uploads/downloads. Authenticated public browser upload, API/browser download,
exact byte comparison, internal-file privacy, and tenant isolation now pass.

[Brevo Free](https://help.brevo.com/hc/en-us/articles/208589409-About-Brevo-s-pricing-plans)
includes 300 emails/day indefinitely without a credit card. Its
[sender setup](https://help.brevo.com/hc/en-us/articles/208836149-Create-a-new-sender-From-name-and-From-email)
verifies an address with an emailed six-digit code. Brevo documents a
[temporary sender replacement](https://help.brevo.com/hc/en-us/articles/14925263522578-Comply-with-Gmail-Yahoo-and-Microsoft-s-requirements-for-email-senders)
for free-email addresses: transactional messages use a Brevo `t-sender-sib.com`
address. This supports basic delivery without DNS access but is a temporary demo
workaround, not a guarantee of inbox placement or a permanent branded sender.
Its [SMTP relay](https://help.brevo.com/hc/en-us/articles/7924908994450-Send-transactional-emails-using-Brevo-SMTP)
uses `smtp-relay.brevo.com`, the SMTP Login, and an SMTP key as the password.
Use STARTTLS on port 2525 because Render Free blocks outbound 25, 465, and 587.
[Transactional activation](https://help.brevo.com/hc/en-us/articles/115000188150-Troubleshooting-Issues-with-Brevo-SMTP)
can require a support ticket from the owner's Brevo account. Activation, Render
outbound sending, and actual inbox delivery are separate checks. Authentication and
the public worker's SMTP submission now pass. The owner confirmed actual Gmail
arrival and successful verification/sign-in on 2026-11-16.

Copy the exact Login field from Brevo's SMTP tab. When SMTP IP blocking is active,
authorize the service's outgoing ranges under **Settings → Security → Authorized
IPs**. The current service reports `74.220.52.0/24` and `74.220.60.0/24`; obtain the
current list from Render's **Connect → Outbound** tab or its
[outbound IP API](https://api-docs.render.com/reference/retrieve-service-outbound-ips).
Brevo's [Authorized IPs page](https://help.brevo.com/hc/en-us/articles/5740111683858-Authorize-and-block-IP-addresses-for-API-and-SMTP-security)
accepts these CIDR entries. `SMTP_FROM` selects the sender; verification recipients
come from each registered account's email address. Check Brevo's transactional logs
when the worker submitted a message but the inbox is still empty.

## Provision and deploy

1. Reuse the existing free Neon database and Render Key Value instance listed above.
   Reuse the verified private Supabase bucket and complete Brevo's sender setup.
   Keep the launch database separate from retained local production demo/test data;
   startup applies any pending schema migrations. Do not reset or import the local
   database. Exact Redis database 0/2 settings and the current provider credentials
   are saved privately in `.env.render.local`; replace only SMTP settings for Brevo.
2. Commit and push verified source to `main` before manually deploying the existing
   Render service. Automatic deployments are off. Local edits are unavailable to
   Render; documentation-only updates require no service deployment.
3. Keep the existing Free Docker service's repository-root build context,
   `docker/Dockerfile.preview`, and `/health/ready` health path. Free services have
   no pre-deploy command or interactive shell; the image's startup performs migrations.
4. Maintain the environment values on that existing service. Never paste credentials
   into committed files, frontend build variables, reports, or chat. `JWT_SECRET`
   is generated by Render and must remain stable on subsequent deployments.

   | Setting | Value |
   |---|---|
   | `DATABASE_URL` | Neon direct connection URL including required TLS parameters |
   | `REDIS_URL` | Internal Render Key Value URL selecting database 0 |
   | `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Same authenticated URL selecting database 2 |
   | `PUBLIC_APP_URL`, `CORS_ORIGINS` | Exact assigned HTTPS Render origin, no path |
   | `S3_ENDPOINT`, `S3_REGION` | Values copied from Supabase's S3 settings |
   | `S3_ACCESS_KEY`, `S3_SECRET_KEY` | Supabase server-only S3 credentials |
   | `SMTP_HOST`, `SMTP_PORT`, `SMTP_STARTTLS` | `smtp-relay.brevo.com`, `2525`, `true` |
   | `SMTP_FROM` | Exact sender address verified by Brevo's email code |
   | `SMTP_USERNAME`, `SMTP_PASSWORD` | Brevo SMTP Login and SMTP key |
   | `AI_API_KEY` | Existing Groq credential if retaining AI generation in v1 |

   Preserve Redis authentication and query parameters when changing the URL's
   database path. Keep `EMBEDDING_API_KEY` empty and knowledge disabled. The source
   blueprint names the generation provider/model that passed earlier live checks;
   public generation through the normal AI endpoints now also passes. Override the
   blueprint's original SMTP2GO host with `SMTP_HOST=smtp-relay.brevo.com`, keep
   `SMTP_PORT=2525` and `SMTP_STARTTLS=true`, and merge SMTP changes into the existing
   service instead of replacing all environment variables.
5. Deploy manually. Confirm migration completion, all worker queues, singleton
   scheduler startup, and `/health/ready`. Check platform memory and CPU graphs.
   The local 512 MiB limit check is a measured demo workload, not a capacity guarantee
   for unrestricted public signups or concurrent password hashing.
6. Verify with controlled real mailboxes: signup without a session, delivered HTTPS
   confirmation, blocked login before confirmation, explicit verification, login,
   reload and cookie rotation, reset, previous-session revocation, and sign-out.
   Repeat role/privacy/realtime/attachment/SLA workflows. Check actual client-IP
   forwarding and login limits on the public Render proxy. Verify Groq generation
   if those controls remain enabled. Knowledge navigation and operations must remain
   disabled. The local Mailpit suite cannot prove external inbox delivery.
7. Test wake-up after idle sleep and access after redeploy. Check Redis/database/storage
   quotas. Export PostgreSQL backups regularly and before releases; restore one into
   an isolated database. Retain a separate copy of attachments too. Free recovery
   windows alone are insufficient for keeping demo data indefinitely.

## Repeat the isolated local check

```powershell
docker compose -f docker-compose.preview.yml up -d --build
cd frontend
npm run test:e2e:preview
cd ..
docker compose -f docker-compose.preview.yml down
```

The preview is reachable at `https://localhost:8443`; Mailpit is bound only to
`127.0.0.1:8027`. The preview container has a hard 512 MiB memory limit and one worker
slot. The same authentication and role workflows used for local production target
this separate stack, with distinct check-account addresses. Test workspaces/history
remain in its own PostgreSQL volume; `down` preserves volumes. Browser checks accept
the local Caddy certificate; public HTTPS/WSS trust needs public-host verification.

The existing [paid Render/Vercel guide](deployment-render-vercel.md) remains a reference
for a later always-on deployment. The current target is the single-origin free demo.
