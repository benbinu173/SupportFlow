import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter, type RouteObject } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionStore } from '@/store/auth'
import type { SessionUser } from '@/types/session'

import { router } from './router'

/**
 * The API client is stubbed for exactly one route, and it is worth being precise
 * about why.
 *
 * Everything else in this file renders without a network layer, which is the
 * point — a test of the route table should not depend on the thing it is not
 * testing. `/dev/health` is the exception, because probing the API *is* what
 * that page does: it fires a `useQuery` at `/health/ready` on mount by design.
 * Left unmocked under jsdom it would attempt a real request to a server that is
 * not running and resolve into an error state that arrives whenever it arrives,
 * which is the kind of test that passes on a fast machine and fails on CI.
 */
vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

/**
 * The route table, rendered through a memory router built from the *same* route
 * objects the application uses.
 *
 * Asserting on `router.routes[0].children` would restate the config; this
 * navigates to a URL and checks which screen answers, which is the thing a user
 * actually experiences.
 *
 * Two pieces of ambient state have to be set for any of this to be meaningful.
 * The session store starts at `unknown`, and *every* guard — including the one
 * in front of the login form — renders a pending state in that condition, so a
 * test that rendered `/login` without touching the store would be asserting
 * against a spinner. `beforeEach` therefore establishes the visitor these cases
 * are actually about: someone with no session. The `unknown` case gets its own
 * test rather than being the accidental default.
 *
 * `SessionProvider` is deliberately not mounted. The bootstrap belongs to it,
 * and a test of the route table should not depend on a session request firing
 * on mount — the pending states below are reached by setting the store, which
 * states the condition each case is about instead of hoping a request that
 * never resolves produces it.
 */

const ANONYMOUS = { status: 'anonymous', user: null, accessToken: null } as const

beforeEach(() => {
  useSessionStore.setState(ANONYMOUS)
})

function renderAt(path: string) {
  const memoryRouter = createMemoryRouter(router.routes, { initialEntries: [path] })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={memoryRouter} />
    </QueryClientProvider>
  )

  return { ...result, memoryRouter }
}

/**
 * Every path the table can match, in declaration order.
 *
 * Written as a walk rather than a hand-kept list because most of the root's
 * children are now *pathless* layout routes — they render a guard or an outlet
 * and have no `path` of their own. Reading `.path` off the first level would
 * report a row of `undefined` and prove nothing; this descends to the leaves,
 * which are the only entries a URL can reach.
 */
function leafPaths(routes: readonly RouteObject[]): string[] {
  return routes.flatMap((route) => [
    ...(route.path === undefined ? [] : [route.path]),
    ...leafPaths(route.children ?? []),
  ])
}

describe('route table', () => {
  it('serves the marketing landing page at the root', async () => {
    renderAt('/')

    // Presence and text, not `toBeVisible`. The headline is rendered inside
    // `Reveal`, which starts at `opacity: 0` and only animates once
    // `IntersectionObserver` reports it on screen — and the stub in
    // `src/test/setup.ts` never fires, because jsdom has no layout and
    // nothing is ever genuinely in the viewport. `toBeVisible` would therefore
    // fail on a page that is entirely correct, since jest-dom counts a
    // zero-opacity ancestor as hidden. Whether a revealed element *finishes*
    // animating is the browser's claim to make; F10's audit is where it gets
    // checked against real rendering.
    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: 'The support desk that shows its working.',
      })
    ).toBeInTheDocument()

    // The public shell and its nav, which are what make this the marketing
    // surface rather than a bare page.
    expect(await screen.findByRole('navigation', { name: 'Main' })).toBeInTheDocument()
  })

  it('serves the sign-in form, inside the auth shell', async () => {
    renderAt('/login')

    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()

    // The controls are asserted by their accessible names rather than merely by
    // the heading: a heading would still render if `Field` stopped wiring
    // `htmlFor` to its child, and an unlabelled login form is unusable with a
    // screen reader.
    //
    // For the email control the name is asserted to be *exactly* `Email`, which
    // is the substantive half of the claim — `Field` renders a decorative `*`
    // for a required control, and it is `aria-hidden` precisely so it stays out
    // of the computed name (a bare asterisk read aloud is punctuation, not
    // information; `aria-required` is what announces the requirement).
    expect(await screen.findByRole('textbox', { name: 'Email' })).toBeInTheDocument()

    // A password input has no implicit ARIA role, so it cannot be reached by
    // role. Matching the label by pattern rather than by exact string is what
    // the asterisk forces: `aria-hidden` keeps it out of the accessible name,
    // but it is still a text node inside the `<label>` element, which is what
    // this query reads. The pattern is anchored because the field also has a
    // reveal button whose `aria-label` ("Show password") would match `/Password/`.
    expect(await screen.findByLabelText(/^Password\*?$/)).toBeInTheDocument()

    // The panel beside the form, and the copy that belongs to *this* route.
    // Asserted here rather than in a test of the panel alone, because the thing
    // that can go wrong is the wiring: `AuthLayout` picks the variant from the
    // path, and a sign-in screen that argues for creating a workspace is a
    // mistake no test of the panel in isolation could see.
    //
    // The heading is the observable, so this does move whenever the marketing
    // copy does. That is the trade, and it is worth it: the alternative is not
    // asserting it at all.
    expect(
      await screen.findByRole('heading', {
        level: 2,
        name: 'Pick up where your queue left off.',
      })
    ).toBeInTheDocument()
  })

  it('serves the registration form', async () => {
    renderAt('/register')

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Create your workspace' })
    ).toBeVisible()

    // By name, because this form has three text boxes and the name is the only
    // thing separating them.
    expect(await screen.findByRole('textbox', { name: 'Organization' })).toBeInTheDocument()

    // The other half of the same wiring claim above: registration must not be
    // handed the sign-in panel.
    expect(
      await screen.findByRole('heading', {
        level: 2,
        name: 'You are about to become its administrator.',
      })
    ).toBeInTheDocument()
  })

  it('renders the not-found page for an unmatched path', async () => {
    renderAt('/no/such/place')

    expect(await screen.findByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible()
  })

  it('serves the component kit on the development route', async () => {
    renderAt('/dev/kit')

    expect(await screen.findByRole('heading', { level: 1, name: 'Component kit' })).toBeVisible()
  })

  it('serves the readiness probe on its development route', async () => {
    renderAt('/dev/health')

    expect(await screen.findByRole('heading', { level: 1, name: 'SupportFlow' })).toBeVisible()

    // The probe is reachable, which is the whole assertion. What it *reports*
    // depends on a live API — that half belongs to the page's own tests, which
    // drive the query directly rather than inferring it from a mount.
    expect(await screen.findByText('Backend status')).toBeInTheDocument()
  })

  it('sends a visitor with no session from a guarded route to the sign-in form', async () => {
    const { memoryRouter } = renderAt('/dashboard')

    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()

    // The URL really moved, rather than the guard merely rendering the form in
    // place. That distinction matters: `replace` here is what keeps the back
    // button from bouncing between two URLs with one screen between them.
    expect(memoryRouter.state.location.pathname).toBe('/login')
  })

  it('remembers where a bounced visitor was going', async () => {
    const { memoryRouter } = renderAt('/dashboard?filter=mine')

    // The path *and* its search string, because dropping the query is how a
    // deep link into a filtered queue comes back as an unfiltered one.
    expect(memoryRouter.state.location.state).toEqual({ from: '/dashboard?filter=mine' })
  })

  it('renders the guarded route rather than a redirect once a session exists', async () => {
    const agent: SessionUser = {
      id: '00000000-0000-0000-0000-000000000001',
      name: 'Avery Chen',
      email: 'avery@example.test',
      role: 'agent',
      is_active: true,
      customer_id: null,
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    }

    useSessionStore.setState({ status: 'authenticated', user: agent, accessToken: 'test-token' })

    renderAt('/dashboard')

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Signed in as Avery Chen' })
    ).toBeVisible()
  })

  it('mounts every route the tests above cover, and no others', () => {
    // The inventory is the guard. A route added without a case here fails this
    // test, which is the point: a growing route table beside a one-line test
    // file is how a guard gets skipped. The paths are asserted rather than the
    // count, so the failure names what was added.
    //
    // `/dev/kit` and `/dev/health` are present because `import.meta.env.DEV` is
    // true under Vitest; the production build drops both, along with the pages
    // behind them.
    const rootRoute = router.routes[0]
    const paths = leafPaths(rootRoute?.children ?? [])

    expect(paths).toEqual([
      '/',
      '/login',
      '/register',
      '/dashboard',
      '/portal',
      '/dev/kit',
      '/dev/health',
      '*',
    ])
  })
})
