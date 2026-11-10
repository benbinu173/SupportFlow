import { render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter, type RouteObject } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'

import { PERMISSIONS, type UserRole } from '@/features/auth/permissions'
import { HOME_BY_ROLE } from '@/routes/home'
import { useSessionStore } from '@/store/auth'
import type { SessionUser } from '@/types/session'

import { RedirectIfAuthenticated, RequireAuth, RequirePermission } from './guards'

/**
 * The guards, exercised apart from the screens they guard.
 *
 * The route table's own test proves *which* screen answers a URL. This one
 * proves what a guard does when it answers with a refusal or a redirect, which
 * is behaviour no screen's content can demonstrate — a page that renders while
 * it should have redirected looks exactly like a page that was always meant to
 * render.
 *
 * The routes here are a fixture rather than the application's table, on purpose:
 * each case is about one guard, and the stand-in pages are one sentence each so
 * a failure names the guard rather than the marketing copy on the page behind
 * it.
 */

function makeUser(role: UserRole): SessionUser {
  return {
    id: '00000000-0000-0000-0000-000000000001',
    name: 'Avery Chen',
    email: 'avery@example.test',
    role,
    is_active: true,
    customer_id: role === 'customer' ? '00000000-0000-0000-0000-0000000000ff' : null,
    created_at: '2026-01-01T00:00:00Z',
    last_login_at: null,
    email_verified_at: '2026-01-01T00:00:00Z',
  }
}

const routes: RouteObject[] = [
  { path: '/login', element: <p>sign-in screen</p> },
  { path: '/dashboard', element: <p>dashboard screen</p> },
  { path: '/portal', element: <p>portal screen</p> },

  {
    element: <RequireAuth />,
    children: [{ path: '/private', element: <p>private screen</p> }],
  },

  {
    element: <RequirePermission permission={PERMISSIONS.AUDIT_VIEW} />,
    children: [{ path: '/audit', element: <p>audit screen</p> }],
  },

  {
    element: <RedirectIfAuthenticated to={(role) => HOME_BY_ROLE[role]} />,
    children: [{ path: '/gate', element: <p>gate screen</p> }],
  },
]

function renderAt(path: string) {
  const memoryRouter = createMemoryRouter(routes, { initialEntries: [path] })

  render(<RouterProvider router={memoryRouter} />)

  return memoryRouter
}

beforeEach(() => {
  useSessionStore.setState({ status: 'unknown', user: null, accessToken: null })
})

describe('RequireAuth', () => {
  it('waits rather than guessing while the session is still unknown', async () => {
    renderAt('/private')

    // `unknown` is the moment between mount and the bootstrap response, when a
    // valid refresh cookie may well be sitting in the browser. Redirecting here
    // is what sends a signed-in user to the sign-in form for a frame and then
    // bounces them back.
    expect(await screen.findByRole('status', { name: 'Checking your session' })).toBeInTheDocument()
    expect(screen.queryByText('private screen')).not.toBeInTheDocument()
  })

  it('sends an anonymous visitor to the sign-in form', async () => {
    useSessionStore.setState({ status: 'anonymous' })

    const memoryRouter = renderAt('/private')

    expect(await screen.findByText('sign-in screen')).toBeInTheDocument()
    expect(screen.queryByText('private screen')).not.toBeInTheDocument()
    expect(memoryRouter.state.location.pathname).toBe('/login')
  })

  it('renders the route once a session exists', async () => {
    useSessionStore.setState({
      status: 'authenticated',
      user: makeUser('agent'),
      accessToken: 'test-token',
    })

    renderAt('/private')

    expect(await screen.findByText('private screen')).toBeInTheDocument()
  })
})

describe('RequirePermission', () => {
  it('refuses in place, without moving the user off the URL they asked for', async () => {
    useSessionStore.setState({
      status: 'authenticated',
      user: makeUser('agent'),
      accessToken: 'test-token',
    })

    const memoryRouter = renderAt('/audit')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'You do not have access to this page'
    )
    expect(screen.queryByText('audit screen')).not.toBeInTheDocument()

    // The refusal renders where the screen would have been. A redirect to the
    // dashboard would be a silent failure: the URL changes to something the
    // user did not ask for and nothing explains why.
    expect(memoryRouter.state.location.pathname).toBe('/audit')
  })

  it('lets a role that holds the permission through', async () => {
    useSessionStore.setState({
      status: 'authenticated',
      user: makeUser('admin'),
      accessToken: 'test-token',
    })

    renderAt('/audit')

    expect(await screen.findByText('audit screen')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('denies when there is no role at all', async () => {
    // Not a hypothetical: an anonymous visitor reaching a permissioned route
    // through a nesting mistake gets `undefined` here, and `can()` resolves an
    // unknown role to denied — the safe failure is a hidden control, never a
    // shown one.
    useSessionStore.setState({ status: 'anonymous' })

    renderAt('/audit')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'You do not have access to this page'
    )
  })
})

describe('RedirectIfAuthenticated', () => {
  it('lets an anonymous visitor reach the screen', async () => {
    useSessionStore.setState({ status: 'anonymous' })

    renderAt('/gate')

    expect(await screen.findByText('gate screen')).toBeInTheDocument()
  })

  it('waits while the session is unknown, rather than flashing the screen', async () => {
    renderAt('/gate')

    expect(await screen.findByRole('status', { name: 'Checking your session' })).toBeInTheDocument()
    expect(screen.queryByText('gate screen')).not.toBeInTheDocument()
  })

  it('sends a signed-in customer to the portal, not to the staff dashboard', async () => {
    useSessionStore.setState({
      status: 'authenticated',
      user: makeUser('customer'),
      accessToken: 'test-token',
    })

    const memoryRouter = renderAt('/gate')

    expect(await screen.findByText('portal screen')).toBeInTheDocument()
    expect(memoryRouter.state.location.pathname).toBe('/portal')
  })

  it('sends a signed-in agent to the dashboard', async () => {
    useSessionStore.setState({
      status: 'authenticated',
      user: makeUser('agent'),
      accessToken: 'test-token',
    })

    const memoryRouter = renderAt('/gate')

    expect(await screen.findByText('dashboard screen')).toBeInTheDocument()
    expect(memoryRouter.state.location.pathname).toBe('/dashboard')
  })
})
