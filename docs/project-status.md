# Project checkpoint

Updated: 2026-11-05.

## Current milestone

F18 frontend browser workflows and CI are complete. This checkpoint is included
in the same commit as the implementation, tests, and workflow fixes.
The preceding committed frontend milestone was `56f2cb9` (F16: entity
audit-history drilldowns).
The backend's A–Z milestones are recorded as complete in the main README.

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
- Vitest: 103 tests passed across 15 files.
- Playwright: 13 browser workflows passed.
- Production build passed. Vite still reports a large JavaScript chunk
  (about 1.34 MB before gzip); route code splitting is a future optimization.

Browser fixtures do not verify a running API, database persistence, or workers.
Backend and deployment checks are separate under `backend/scripts/`.

## Next recommended step

Exercise the role workflows against a running backend: manager assignment, agent
replies and resolution, customer close/reopen, and realtime delivery. Use the
main README's local setup and walkthrough commands. Choose the next feature
milestone after this integration check; no later frontend milestone is defined
in the repository.
