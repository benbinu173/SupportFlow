# Free demo deployment

Updated: 2026-11-16. The owner selected a single Render URL for the frontend, API,
and WebSockets. Hosting must cost zero and require no payment card. RAG is deferred
to v2. This is a small demo with free-plan availability limits.

## Prepared source

- `render.yaml` declares one **Free** Docker web service in Singapore. Automatic
  deployments are off. The platform assigns its actual `*.onrender.com` hostname;
  `supportflow-demo.onrender.com` is a suggested name, subject to availability.
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
This verifies local dependencies and Mailpit, not Neon, Supabase, SMTP2GO, or public TLS.

## External free services

| Component | Candidate | Configuration |
|---|---|---|
| PostgreSQL | Neon Free | PostgreSQL 17, direct TLS connection URL, `vector` and `pg_trgm` extensions |
| Redis | Render Free Key Value | Same Singapore region, internal URL, `noeviction`; database 0 for cache and 2 for Celery |
| Files | Supabase Free Storage | Private `supportflow-attachments` bucket; S3 endpoint, region, server-only S3 access keys |
| Email | SMTP2GO Free | Verify a single sender address you own, `mail.smtp2go.com:2525`, STARTTLS, SMTP user/password |
| AI generation | Existing Groq free access | `AI_PROVIDER=groq`, `AI_MODEL=openai/gpt-oss-120b`; use the existing private generation credential |

Neon and Redis are already provisioned and authenticated account reads verified:

- Neon **supportflow-demo**, project `cold-term-95520688`, Free, PostgreSQL 17,
  Singapore (`aws-ap-southeast-1`). Use the **production** branch
  (`br-icy-leaf-a1rznszb`) and database **supportflow** for launch. Its direct TLS
  URL is saved in `.env.render.local`. The launch database is deliberately empty.
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

The owner verified an SMTP2GO single sender and saved the SMTP credentials privately
in `.env.render.local`. The preview image passed SMTP authentication on port 2525
over verified TLS 1.3 on 2026-11-16. No email was sent by that probe; Render outbound
access and external inbox delivery still need public-host verification.

Reuse these resources. Storage and the Render web service still need completion.
Choose only Free plans requiring no payment card.

The Render and Neon MCP tools work in this session. Supabase's URL had a literal
`YOUR_PROJECT_REF`; it is corrected to `https://mcp.supabase.com/mcp?read_only=true`
with a new completed OAuth sign-in. Restart the Codex IDE extension to load its
tools and inspect the account. The connection remains read-only, so missing
Supabase storage must be provisioned in its Dashboard unless a write connection
is explicitly authorized. No SupportFlow storage project or bucket is verified.
These are local MCP connections; plugin-directory installation state is separate.

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
uploads/downloads. The local MinIO check does not verify Supabase compatibility.

[SMTP2GO Free](https://www.smtp2go.com/pricing/) advertises no credit card requirement.
Its [free plan](https://support.smtp2go.com/hc/en-gb/articles/223087947-Free-Plan) allows
1,000 emails monthly and 200 daily, including up to five verified domains or single
senders. A verified single address avoids buying a domain. Its
[SMTP settings](https://support.smtp2go.com/hc/en-gb/articles/223087627-SMTP-Settings)
support STARTTLS on port 2525; Render blocks outbound ports 25, 465, and 587.
Actual port access and inbox delivery require a public-host check.

## Provision and deploy

1. Reuse the existing free Neon database and Render Key Value instance listed above.
   Complete the private Supabase storage bucket and verified SMTP2GO sender.
   Keep the launch database separate from retained local production demo/test data;
   startup runs the first schema migration. Do not reset or import the local database.
   The owner's verified SMTP credentials and the exact Redis database 0/2 settings
   are already saved privately in `.env.render.local`.
2. Commit and push the verified source to the intended GitHub branch before importing
   the Render Blueprint. Local edits and commits ahead of the remote are unavailable
   to Render. Publishing source is still a separate operator action.
3. In Render, import `render.yaml` or create a Free Docker web service manually with
   repository-root context and Dockerfile `docker/Dockerfile.preview`. Use
   `/health/ready` as the health path. Free services have no pre-deploy command or
   interactive shell; the image's startup performs migrations.
4. Fill the `sync: false` environment values in Render. Never paste credentials
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
   | `SMTP_FROM` | Verified single sender address |
   | `SMTP_USERNAME`, `SMTP_PASSWORD` | SMTP credentials, not the website account password |
   | `AI_API_KEY` | Existing Groq credential if retaining AI generation in v1 |

   Preserve Redis authentication and query parameters when changing the URL's
   database path. Keep `EMBEDDING_API_KEY` empty and knowledge disabled. The source
   blueprint names the generation provider/model that passed earlier live checks;
   that does not verify today's public provider access or quota.
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
