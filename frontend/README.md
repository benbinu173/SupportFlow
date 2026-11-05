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

HTTP responses and WebSocket frames use isolated, deterministic fixtures typed
against the generated API schemas. These checks exercise the browser interface
without a local backend or demo database. Backend authorization, tenant isolation,
workers, and persistence are covered by the backend suite and walkthrough scripts.
An unhandled API request or uncaught browser error fails the test.

`npm test` runs the Vitest component and service tests separately.
`npm run typecheck` checks both application and browser-test code. CI runs both suites and
uploads browser traces, screenshots, and the HTML report on failure. Local reports
are available with `npx playwright show-report`; test output is ignored by Git.
