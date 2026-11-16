# SupportFlow v1 deployment: Render and Vercel

The owner subsequently specified a zero hosting budget and no payment card on 2026-11-12.
The paid managed-service setup below is a reference only. Follow the current free-preview
planning checkpoint in [project-status.md](project-status.md#next-recommended-step) instead.

Recommended on 2026-11-12: use Render for the Docker API, background worker, scheduler,
Postgres, and Redis-compatible Key Value; use Vercel for the React frontend; use managed
private object storage and an SMTP provider. This uses platforms the project owner already
knows while keeping email jobs and SLA scheduling running independently of HTTP traffic.

Render supports building the existing Dockerfile. Its services are configured separately;
`docker-compose.prod.yml` remains the local/VPS deployment configuration. Render supplies
public HTTPS and WSS for the API, so this route does not deploy the local Caddy/nginx stack.
See [Docker on Render](https://render.com/docs/docker) and
[Render service types](https://render.com/docs/service-types).

This document is a deployment procedure, not a record of a public deployment. The local
production stack passed authentication, migration, browser, and TLS checks. Public domains,
managed storage, Render service configuration, Vercel headers, and real email delivery must
be configured and verified on the target platforms.

| Component | Deployment target | Purpose |
|---|---|---|
| React frontend | Vercel | Static production bundle and frontend domain |
| FastAPI | Render Docker Web Service | HTTP API and authenticated WebSockets |
| Celery worker | Render Docker Background Worker | Verification/reset email, notifications, and AI jobs |
| Celery beat | Separate Render Docker Background Worker | SLA and maintenance scheduling |
| PostgreSQL | Paid Render Postgres | Tenant data, sessions, audit history, and migrations |
| Redis-compatible store | Paid Render Key Value | Cache, realtime fan-out, and durable job queue |
| Attachments | Private AWS S3 or Cloudflare R2 bucket | Persistent files, served through authorized API routes |
| Email | SMTP provider with verified sender | Deliver verification, recovery, and notification messages |

A VPS remains an alternative when operating Linux and taking responsibility for backups,
patches, and service monitoring is acceptable. It fits the already verified Compose/Caddy
configuration closely. The managed Render/Vercel route is recommended here for familiarity
and reduced server administration; individual paid service charges must be budgeted.

1. **Choose the production domains and service budget.**

   Use two subdomains of one domain you own, for example:

   ```text
   Frontend: https://app.yourdomain.com
   API:      https://api.yourdomain.com
   Socket:   wss://api.yourdomain.com/ws
   ```

   The project uses a host-only HttpOnly refresh cookie with `SameSite=Lax`. The browser can
   send that API cookie on requests from the HTTPS frontend when the two subdomains are
   same-site. `your-app.vercel.app` and `your-api.onrender.com` are different sites; simply
   adding CORS does not make the existing cookie policy work across them. Use the custom
   domains for the production authentication checks. See
   [MDN's cookie guide](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Cookies).

   Plan for paid backend services and datastores. Background workers have no free compute
   option; free web services sleep and free datastores have production limitations. Paid
   Postgres supplies recovery, and paid Key Value supplies persistence. Choose current
   sizes/prices in [Render compute plans](https://render.com/docs/compute-plans) and
   [Render pricing](https://render.com/pricing), then check worker memory under realistic load.
   See [Render free-plan limitations](https://render.com/docs/free).

2. **Prepare and publish the v1 source.**

   Hide or disable deferred knowledge ingestion/search entry points and their API operations.
   Keep the schema and implementation available for v2. An empty embedding key alone does
   not remove the currently visible knowledge controls.

   Add the Render service configuration and a Vercel SPA/header configuration. Adapt the
   public browser checks to the chosen frontend/API origins and mailboxes you control.
   These changes are still pending; they were not part of the local production verification.

   Run the relevant checks, commit the release changes, and push the verified branch to GitHub
   before importing it on the platforms. At this checkpoint, the local branch has commits
   ahead of its tracked remote plus uncommitted verification/documentation changes. Do not
   assume Render or Vercel can build those local-only changes.

   Start with a fresh public database. The local production database contains demo/check
   tenants and histories; do not run `seed_demo --reset` or import that data into the launch
   database unless a deliberate demo deployment is intended.

3. **Provision PostgreSQL and Key Value on Render.**

   Create Postgres and Key Value in the same Render region as all three backend processes.
   Choose a region near the expected users, such as Singapore for an audience mainly in India.
   Use the internal connection URLs for the API, worker, and scheduler.

   Prefer PostgreSQL 17 to match the tested stack. Keep `vector` and `pg_trgm` available:
   the existing migrations need them even with RAG deferred. The migrations enable them;
   Render supports both. See
   [Render Postgres extensions](https://render.com/docs/postgresql-extensions).

   For Key Value, use `noeviction` and Journal + Snapshot persistence for the job queue.
   Monitor memory because `noeviction` returns write errors when capacity is exhausted.
   Keep database 0 for cache/pub-sub and database 2 for the Celery broker/result backend;
   validate both logical database connections during setup. Retain authentication/query
   parameters when setting the database suffix in the connection URLs. See
   [Render Key Value configuration](https://render.com/docs/key-value#configure-your-key-value-instance).

4. **Provision private file storage and email delivery.**

   Create a private S3-compatible bucket. AWS S3 and Cloudflare R2 are options; R2 exposes
   an [S3-compatible API](https://developers.cloudflare.com/r2/api/s3/api/). Supply that
   provider's endpoint, credentials, bucket name, and region. Test attachment upload and
   authorized download with the chosen provider before launch.

   Files already travel through FastAPI, so the bucket does not need public access and
   frontend code does not need storage credentials. Use provider access limited to the
   required bucket operations. The public route uses managed storage instead of the local
   MinIO container; Render's application filesystem is not an attachment store.

   Choose an SMTP provider, verify its sending domain/address, and add the provider's DNS
   records. Obtain its host, port, username, password, and STARTTLS setting. Use SMTP because
   that is the application's existing email transport. Production mail must arrive in real
   mailboxes; the Mailpit override is only for local checks.

5. **Create a shared backend environment configuration.**

   Store runtime secrets in Render environment settings or an environment group shared by
   the API, worker, and scheduler. Use the same JWT secret, database, broker, public frontend
   origin, and mail configuration across those processes. Use a newly generated production
   signing secret. Do not put secrets in Vercel `VITE_*` settings or commit real env files.

   The principal values are:

   | Setting | Production value |
   |---|---|
   | `ENVIRONMENT` | `production` |
   | `DEBUG` | `false` |
   | `LOG_LEVEL` | `INFO` |
   | `DATABASE_URL` | Render's internal Postgres URL |
   | `REDIS_URL` | Internal Key Value URL selecting database 0 |
   | `CELERY_BROKER_URL` | Internal Key Value URL selecting database 2 |
   | `CELERY_RESULT_BACKEND` | Internal Key Value URL selecting database 2 |
   | `JWT_SECRET` | Generated production secret, shared by backend processes |
   | `CORS_ORIGINS` | `https://app.yourdomain.com` |
   | `PUBLIC_APP_URL` | `https://app.yourdomain.com` |
   | `SMTP_HOST`, `SMTP_PORT` | The selected provider's SMTP endpoint |
   | `SMTP_USERNAME`, `SMTP_PASSWORD` | Provider credentials |
   | `SMTP_FROM` | Verified sender address |
   | `SMTP_STARTTLS` | Provider's documented setting, usually `true` on port 587 |
   | `S3_ENDPOINT`, `S3_REGION` | Selected object-storage endpoint and region |
   | `S3_BUCKET` | Private attachment bucket |
   | `S3_ACCESS_KEY`, `S3_SECRET_KEY` | Storage credentials |
   | `AI_PROVIDER` | `groq` if retaining the previously verified generation provider |
   | `AI_MODEL` | `openai/gpt-oss-120b` for that verified Groq configuration |
   | `AI_API_KEY` | Generation credential if v1 includes summaries/classification/drafts |
   | `EMBEDDING_API_KEY` | Empty while RAG is deferred and its operations disabled |

   Keep the remaining auth, queue, timeout, and SLA settings from the production example
   unless there is a reason to tune them. Plain `postgresql://` URLs are normalized to the
   installed psycopg driver by the app. The local example's `postgres`, `redis`, and `minio`
   hostnames are Compose service names and must be replaced for Render.

6. **Deploy the FastAPI Docker Web Service.**

   In Render, create a Web Service linked to the verified GitHub branch and select Docker.
   With the repository root as the service root, set:

   | Field | Value |
   |---|---|
   | Dockerfile Path | `backend/Dockerfile` |
   | Docker Build Context Directory | `backend` |
   | Runtime stage | Existing Dockerfile's final `production` stage |
   | Docker Command | Leave empty to use the existing production `CMD` |
   | Environment | Shared backend configuration, plus `PORT=8000` |
   | Pre-deploy Command | `alembic upgrade head` |
   | Health Check Path | `/health/ready` |
   | Custom Domain | `api.yourdomain.com` |

   The existing image binds to `0.0.0.0:8000`. Render permits overriding `PORT` to match it;
   see [Render port binding](https://render.com/docs/web-services#port-binding). A paid service
   supports the [pre-deploy migration command](https://render.com/docs/deploys#pre-deploy-command).

   Deploy the API first, inspect the migration result, and verify `/health` and `/health/ready`.
   The API may start before the email worker, but registration is not ready for use until
   the worker and SMTP delivery have been verified.

7. **Deploy the Celery worker and singleton scheduler.**

   Create two Docker Background Worker services, using the same Dockerfile/build context
   and shared environment. Override each Docker Command:

   Worker:

   ```text
   celery -A app.workers.celery_app worker --concurrency=2 --loglevel=info -Q notifications,sla,ai,knowledge,maintenance
   ```

   Scheduler:

   ```text
   celery -A app.workers.celery_app beat --loglevel=info -s /tmp/celerybeat-schedule
   ```

   Two worker slots are a starting point for modest traffic, not a measured capacity
   guarantee. Select/adjust its plan and concurrency after checking memory and queue latency.
   Deploy these processes only after the API's migration succeeds. Do not attach simultaneous
   Alembic runs to all three services.

   Keep the scheduler at one instance with autoscaling disabled, and coordinate replacement
   so two beat processes do not overlap. Inspect worker logs to confirm all five queues are
   consumed and scheduler logs to confirm periodic SLA tasks are dispatched. Render supports
   [continuous background workers](https://render.com/docs/background-workers), and
   [Celery requires a single scheduler](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html).

8. **Deploy the frontend on Vercel.**

   Import the same verified repository. Set:

   | Field | Value |
   |---|---|
   | Root Directory | `frontend` |
   | Framework Preset | Vite |
   | Install Command | `npm ci` |
   | Build Command | `npm run build` |
   | Output Directory | `dist` |
   | `VITE_API_BASE_URL` | `https://api.yourdomain.com` |
   | Production Domain | `app.yourdomain.com` |

   The API base value is the origin only: the clients add `/api/v1`. The realtime client
   derives `wss://api.yourdomain.com/ws` from the same variable; this project does not need
   a separate WebSocket variable. Vite embeds this value at build time, so changing it
   requires a fresh frontend deployment.

   Before deployment, add `frontend/vercel.json` with the SPA deep-link fallback:

   ```json
   {
     "$schema": "https://openapi.vercel.sh/vercel.json",
     "rewrites": [
       { "source": "/(.*)", "destination": "/index.html" }
     ]
   }
   ```

   Extend this configuration with the existing frontend security and caching headers.
   Its document CSP must allow `https://api.yourdomain.com` and `wss://api.yourdomain.com`
   in `connect-src`; the current nginx policy only allows the same origin. Keep the external
   theme bootstrap working under CSP and preserve document/theme revalidation plus immutable
   hashed-asset caching. The nginx header file is not automatically applied on Vercel.
   The precise header configuration must be prepared for the actual domain. See
   [Vite on Vercel](https://vercel.com/docs/frameworks/frontend/vite) and
   [Vercel response headers](https://vercel.com/docs/headers).

9. **Connect DNS and validate the public origins.**

   Add each subdomain to its platform and use the exact DNS records the platform provides.
   Wait for both domains and their HTTPS certificates to validate. Add the SMTP provider's
   verification records as well. Verify that browser API requests use the API custom domain
   and email links use the frontend custom domain.

   The React app opens authenticated WebSockets directly against the Render API. Render
   supports public [WSS connections](https://render.com/docs/websocket). Public browser checks
   must cover cross-origin CORS and cookie behavior, which the previous single-origin local
   Caddy run did not exercise. Keep staging/preview origins explicitly configured separately;
   a random Vercel preview hostname is not a substitute for the same-site production domain.

10. **Verify the actual deployment before opening it to users.**

    Use real email addresses and a dedicated check workspace. Verify signup, delivered
    confirmation, blocked login before verification, sign-in/reload, resend, used-link
    rejection, password reset, old-session revocation, and sign-out. Repeat role/ticket
    workflows, private-note isolation, realtime updates, attachments, and scheduled SLA
    notification delivery. Verify the chosen generation provider if AI features ship in v1.

    Existing local production browser helpers use localhost and Mailpit with example-domain
    accounts. They need adaptation for public origins and real controlled mailboxes; running
    the unchanged localhost suite does not verify this public split-host deployment. Likewise,
    the existing smoke checker defaults to demo credentials and a Mailpit-provisioned second
    tenant, so it needs public check-account configuration rather than demo seeding.

    Verify frontend deep-link reloads, actual CSP, secure refresh-cookie flags and rotation,
    CORS credentials, valid HTTPS/WSS certificates, and tenant isolation on the public URLs.

11. **Confirm recovery and monitor the release.**

    Verify the paid Postgres recovery window and export backups as needed. Test restoration
    into a separate database. Configure file retention/recovery for the object bucket too;
    restoring PostgreSQL does not restore deleted attachments. Render documents its
    [Postgres recovery and backup capabilities](https://render.com/docs/postgresql-backups).

    Monitor API readiness/errors, worker failures and queue latency, scheduler activity,
    datastore usage, and email delivery failures. Keep the deployed commit/image version
    recorded. Later releases should migrate once before worker/scheduler updates and publish
    the compatible frontend only after the backend is ready. A code rollback across a
    schema change needs an explicit database compatibility/recovery decision.

The first inputs needed are the actual domain, an acceptable monthly service budget,
the object-storage/SMTP providers, and access to the existing Render and Vercel projects.
No public services, billable resources, domain records, or remote secrets were changed
while preparing this guide.
