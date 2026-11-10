import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configure, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { router } from '@/routes/router'
import { ApiError, ERROR_CODES } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import type { SessionUser } from '@/types/session'

// Route modules now load during rendering. Allow their imports to settle on
// slower Windows filesystems before asserting on the resulting screen.
configure({ asyncUtilTimeout: 10_000 })

// Browser tests cover chunk delays and failures. Prepare the modules used by
// these form-flow tests before their interaction timeout starts.
beforeAll(async () => {
  await Promise.all([
    import('@/layouts/AuthLayout'),
    import('@/pages/auth/LoginPage'),
    import('@/pages/HomePage'),
  ])
}, 30_000)

/**
 * Signing in, through the real route table.
 *
 * F2's demo is "log in as a seeded user and land on your role's home", and this
 * is that claim written down: the real form, the real guard chain, the real
 * session store, and only the network replaced. What it therefore proves is the
 * seam the phase is actually about — that `/auth/login` yields a token,
 * `/auth/me` yields the role, and the role is what decides where the user ends
 * up. A test that mocked `useAuthenticate` would prove the form calls a
 * function, which is the part that was never in doubt.
 */

const api = vi.hoisted(() => ({
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  fetchCurrentUser: vi.fn(),
  bootstrapSession: vi.fn(),
  bootstrapSessionOnce: vi.fn(),
}))

vi.mock('@/services/api/auth', () => api)

const AGENT: SessionUser = {
  id: '00000000-0000-0000-0000-000000000001',
  name: 'Avery Chen',
  email: 'avery@example.test',
  role: 'agent',
  is_active: true,
  customer_id: null,
  created_at: '2026-01-01T00:00:00Z',
  last_login_at: null,
  email_verified_at: '2026-01-01T00:00:00Z',
}

const TOKEN = {
  access_token: 'issued-access-token',
  token_type: 'bearer',
  expires_in: 900,
}

function renderLogin() {
  const memoryRouter = createMemoryRouter(router.routes, { initialEntries: ['/login'] })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={memoryRouter} />
    </QueryClientProvider>
  )

  return memoryRouter
}

/** Fill and submit, the way a person does it. */
async function signIn(email = AGENT.email, password = 'correct-horse-battery') {
  const user = userEvent.setup()

  await user.type(await screen.findByRole('textbox', { name: 'Email' }), email)
  // The whole label, asterisk and all, rather than `/Password/` anywhere. The
  // field carries a reveal button labelled "Show password", and
  // `getByLabelText` matches an `aria-label` as readily as it matches a
  // `<label>` — so the loose pattern addresses two controls and throws. The
  // asterisk is present because `Field` renders one inside the label for a
  // required control even though `aria-hidden` keeps it out of the computed
  // accessible name.
  await user.type(screen.getByLabelText(/^Password\*?$/), password)
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
}

beforeEach(() => {
  vi.clearAllMocks()

  // Cleared so the web-storage assertion below is about what *this* flow wrote,
  // rather than about whatever a previous case happened to leave behind.
  window.localStorage.clear()
  window.sessionStorage.clear()

  // `anonymous` rather than the store's initial `unknown`: the point of these
  // cases is a visitor who can actually see the form.
  useSessionStore.setState({ status: 'anonymous', user: null, accessToken: null })

  api.login.mockResolvedValue(TOKEN)
  api.fetchCurrentUser.mockResolvedValue(AGENT)
})

describe('signing in', () => {
  it('ends on the home its role belongs to, holding the token the server issued', async () => {
    const memoryRouter = renderLogin()

    await signIn()

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Welcome back, Avery' })
    ).toBeVisible()

    expect(memoryRouter.state.location.pathname).toBe('/dashboard')

    expect(api.login).toHaveBeenCalledTimes(1)
    expect(api.login).toHaveBeenCalledWith({
      email: AGENT.email,
      password: 'correct-horse-battery',
    })

    // The profile is fetched rather than taken from the login response, because
    // `/auth/login` returns a token and nothing else — so this call is the only
    // place the app can learn what role it is holding.
    expect(api.fetchCurrentUser).toHaveBeenCalledTimes(1)

    // The token is in the store and nowhere else. Asserting it landed somewhere
    // is the half of the memory-only decision this test can check; that it did
    // not land in `localStorage` is checked below.
    expect(useSessionStore.getState().accessToken).toBe('issued-access-token')
    expect(useSessionStore.getState().status).toBe('authenticated')
  })

  it('has the token in the store before it asks who the token belongs to', async () => {
    // The request interceptor authorises `/auth/me` out of the session store
    // rather than from an argument, so recording the token and fetching the
    // profile in the wrong order sends the request with no `Authorization`
    // header — and the real server answers that 401, which the interceptor
    // treats as an expired session.
    //
    // Every other case in this file mocks `fetchCurrentUser` with a bare
    // `mockResolvedValue`, and a mock answers no matter who calls it or with
    // what headers. That is why none of them could see this: the ordering is
    // only observable from *inside* the call, which is where this reads it.
    let tokenAtFetch: string | null | 'never called' = 'never called'

    api.fetchCurrentUser.mockImplementation(() => {
      tokenAtFetch = useSessionStore.getState().accessToken

      return Promise.resolve(AGENT)
    })

    renderLogin()

    await signIn()

    await screen.findByRole('heading', { level: 1, name: 'Welcome back, Avery' })

    expect(tokenAtFetch).toBe('issued-access-token')
  })

  it('puts no long-lived credential in web storage', async () => {
    renderLogin()

    await signIn()

    await screen.findByRole('heading', { level: 1, name: 'Welcome back, Avery' })

    // The backend issues the refresh token as `HttpOnly` specifically so that a
    // cross-site-scripting flaw cannot read it. A client that copied the access
    // token into `localStorage` would hand that same flaw a usable credential
    // for as long as the tab lived, which is the decision this guards.
    expect(Object.keys(window.localStorage)).toHaveLength(0)
    expect(Object.keys(window.sessionStorage)).toHaveLength(0)
  })

  it('reports bad credentials on the form, and stays put', async () => {
    api.login.mockRejectedValue(
      new ApiError({
        message: 'Incorrect email or password.',
        code: ERROR_CODES.INVALID_CREDENTIALS,
        status: 401,
      })
    )

    const memoryRouter = renderLogin()

    await signIn()

    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect email or password.')
    expect(memoryRouter.state.location.pathname).toBe('/login')
    expect(useSessionStore.getState().status).toBe('anonymous')
  })

  it('renders a field error beside the input, not in the banner', async () => {
    api.login.mockRejectedValue(
      new ApiError({
        message: 'The request could not be processed.',
        code: ERROR_CODES.VALIDATION_ERROR,
        status: 422,
        fieldErrors: [{ field: 'email', message: 'Enter a valid email address.' }],
      })
    )

    renderLogin()

    await signIn()

    // `AuthAlert` suppresses itself when there are field errors, because the
    // same news in two places puts the copy the user needs furthest from the
    // control that fixes it.
    expect(await screen.findByText('Enter a valid email address.')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('tells a rate-limited user how long to wait', async () => {
    api.login.mockRejectedValue(
      new ApiError({
        message: 'Too many requests.',
        code: ERROR_CODES.RATE_LIMITED,
        status: 429,
        retryAfterSeconds: 42,
      })
    )

    renderLogin()

    await signIn()

    // The countdown rather than the generic message, because "wait 42 seconds"
    // is actionable and "something went wrong" invites the one action that is
    // guaranteed not to work: pressing the button again.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Too many attempts. Try again in 42 seconds.'
    )
  })

  it('leaves the session alone when the profile call fails after a good token', async () => {
    api.fetchCurrentUser.mockRejectedValue(
      new ApiError({
        message: 'Could not reach the server.',
        code: ERROR_CODES.NETWORK_ERROR,
        status: null,
      })
    )

    renderLogin()

    await signIn()

    // A token that arrived but could not be resolved into a user has to leave
    // the app in exactly the state it started in. Recording a half-session here
    // is how a reload lands a user on a dashboard whose every request 401s.
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server.')
    expect(useSessionStore.getState().status).toBe('anonymous')
    expect(useSessionStore.getState().accessToken).toBeNull()
  })
})
