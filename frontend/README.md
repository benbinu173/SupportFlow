SupportFlow frontend. See the [root README](../README.md) for setup and
architecture. Commands: `npm run dev`, `npm run lint`, `npm run typecheck`,
`npm test`, `npm run build`.

## Browser workflow checks

F18 automates the role workflows previously checked manually. See
[the project checkpoint](../docs/project-status.md) for the current state and next step.

Install Chromium once with `npx playwright install chromium`, then run
`npm run test:e2e`. Playwright builds the app and starts a preview server at
`http://127.0.0.1:4173` automatically. It checks the production bundle, including
role guards and the absence of developer routes.

The workflows cover sign-in for all four roles, admin account creation, manager
assignment and priority changes, agent replies and internal notes, the ticket
lifecycle (including returning agents to the queue when reopening clears their
assignment), customer close/reopen and replies, the customer portal, AI draft
review, failed-reply recovery, session
restoration, profile navigation and sign-out, realtime replies, and reconnect recovery.
Knowledge workflows also verify that returning to analytics refreshes usage after
successful and failed questions, and after source processing completes or fails.
Password recovery checks cover the sign-in link, conditional email confirmation,
fragment removal, password confirmation, successful reset, invalid links, and mobile layout.
Email verification checks cover signup without a session, resend, explicit confirmation,
fragment removal, reused/expired links, verification-driven sign-in, and phone layouts.

HTTP responses and WebSocket frames use isolated, deterministic fixtures typed
against the generated API schemas. These checks exercise the browser interface
without a local backend or demo database. Backend authorization, tenant isolation,
workers, and persistence are covered by the backend suite and walkthrough scripts.
An unhandled API request or uncaught browser error fails the test.

`npm test` runs the Vitest component and service tests separately.
`npm run typecheck` checks both application and browser-test code. CI runs both suites and
uploads browser traces, screenshots, and the HTML report on failure. Local reports
are available with `npx playwright show-report`; test output is ignored by Git.

## Live backend browser checks

`npm run test:e2e:live` checks the production frontend against the real local API.
Start the infrastructure with `docker compose up -d`, then run the API and Celery
worker using the main README's commands (`make api` and `make worker` also work
from Git Bash). The API must listen on port 8000 and Mailpit on port 8025; the
worker must consume `notifications`. No scheduler is needed for these workflows.

If a parent terminal defines `DEBUG` as a non-boolean value, set it to `false`
for the API and worker processes. This check does not require an AI provider key.
The SMTP configuration must point to the local Mailpit service.
Set `PUBLIC_APP_URL=http://127.0.0.1:4174` for the API and email worker so delivered
recovery and verification links open the preview used by this suite. Set these on the test processes;
normal development can retain `http://localhost:5173`.

The command builds the frontend and serves it at `http://127.0.0.1:4174`, using
Vite's existing HTTP and WebSocket proxies. It performs no response or socket
mocking. Separate browser sessions sign in as manager, agent, and customer and
verify assignment, public replies, internal-note privacy, resolution,
close/reopen, session restoration, and agent access after assignment is cleared.
Realtime assertions read the recipient's screen without reloading. Email checks
read Mailpit and match both the recipient and the current ticket reference.

The recovery check creates a unique agent in that tenant, requests an email from the
browser, and follows its unaltered fragment link from Mailpit. It confirms that previous
browser sessions, access tokens, refresh cookies, and reused links are refused, then
signs in and reloads with the replacement password. The generated agent is deactivated
afterward; existing users and mailbox messages are retained. Desktop/mobile screenshots
and a sanitized verification record are saved. Credential-bearing traces are disabled
for this check. To run only recovery, use
`npm run test:e2e:live -- password-reset.spec.ts`.

The verification check creates an unverified agent and proves sign-in is blocked before
confirmation. It follows the delivered Mailpit link, checks that opening it alone does
not verify the account, confirms explicitly, rejects replay, and signs in and reloads.
Desktop/phone screenshots and a sanitized record are retained; credential-bearing traces
are disabled. Run it with `npm run test:e2e:live -- email-verification.spec.ts`.
Shared setup verifies newly generated accounts through Mailpit before logging in,
including in the recovery and role workflows. The mailbox and worker are required even
when only the role workflow is selected.

The checks create and reuse a dedicated **Local live browser checks** tenant
with accounts under `supportflow-live.example.com` and create fresh tickets per
run. The local test password is defined in `e2e-live/helpers.ts`. Existing
data is retained so results can be inspected, and registration/login limits still
apply. Run this command against the local development stack.

Live reports are stored under `playwright-report/live` and `test-results/live`,
both ignored by Git. The deterministic F18 suite continues to run with
`npm run test:e2e`; the live suite is a separate local integration check.

## Production stack browser checks

`npm run test:e2e:production` runs the live role workflows against the local
Docker production stack at `https://localhost`. It starts no Vite server:
browser traffic and test setup/readback use Caddy's HTTPS origin, and realtime
connections use WSS. A separate admin workflow checks secure HttpOnly refresh
cookies, rotation on reload, a lazy-loaded preferences screen, CSP errors,
deep-link session restoration, and sign-out.

From the project root, start the stack with the local mail-capture override:

```powershell
docker compose --env-file .env.production -f docker-compose.prod.yml -f docker-compose.browser-checks.yml up -d --build
Push-Location frontend
try { npm run test:e2e:production } finally { Pop-Location }
```

The override routes backend/worker SMTP to Mailpit on the compose network and
exposes its inspection API only at `127.0.0.1:8026`. It changes neither the
production env file nor the database volumes. Use it only for local verification.
The suite creates/reuses **Local production browser checks**, with accounts
under `supportflow-production-browser.example.com`, and retains test tickets.

Chromium accepts Caddy's local certificate with `ignoreHTTPSErrors`; certificate
chain and hostname validation remain a separate `deploy_smoke --verify-tls`
check, documented in the root README. The suite does not configure AI providers.
Reports and traces are under `playwright-report/production` and
`test-results/production`, both gitignored. To restore the regular production
stack and remove the temporary Mailpit container after verification:

```powershell
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --remove-orphans
```

## Route loading and bundle size

Product screens and the auth/marketing layouts load through `React.lazy`.
Session and permission guards run before protected screens download. A screen
that is still downloading shows an accessible `Loading page` status; a failed
download uses the existing error page, where Reload retries the application.

The production build emits `dist/.vite/manifest.json`, which records each
screen's chunk and its static dependencies. Count the entry and all its static
imports when measuring startup JavaScript; the size of `index-*.js` alone omits
shared chunks. The project checkpoint records the before/after measurements.

The deterministic browser suite also checks protected-screen downloads,
delayed loading, and recovery from a failed screen download.
