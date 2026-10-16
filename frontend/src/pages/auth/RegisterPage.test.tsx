import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { router } from '@/routes/router'
import { ApiError, ERROR_CODES } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import type { SessionUser } from '@/types/session'

/**
 * Creating a workspace, through the real route table.
 *
 * Registration is the opposite transaction to sign-in and worth its own file for
 * one reason: it creates a *tenant*, not a user, and the account it produces is
 * necessarily that tenant's administrator. The wire payload is therefore the
 * contract — `organization_name` in snake case, exactly as `RegisterRequest`
 * spells it — and getting it wrong is not a typo but a 422 on a form whose user
 * has not yet got an account to complain from.
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

/** The registered account is the new tenant's admin, so that is the home. */
const ADMIN: SessionUser = {
  id: '00000000-0000-0000-0000-000000000002',
  name: 'Dana Whitfield',
  email: 'dana@northwind.example.com',
  role: 'admin',
  is_active: true,
  customer_id: null,
  created_at: '2026-01-01T00:00:00Z',
  last_login_at: null,
}

const TOKEN = {
  access_token: 'issued-access-token',
  token_type: 'bearer',
  expires_in: 900,
}

const FORM = {
  organization: 'Northwind Trading',
  name: 'Dana Whitfield',
  email: 'dana@northwind.example.com',
  password: 'correct-horse-battery',
} as const

function renderRegister() {
  const memoryRouter = createMemoryRouter(router.routes, { initialEntries: ['/register'] })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={memoryRouter} />
    </QueryClientProvider>
  )

  return memoryRouter
}

/**
 * `FORM` is `as const` so the assertions below read as the literal strings the
 * page must send. That makes `Partial<typeof FORM>` a union of those same
 * literals, which is no use for a parameter whose whole purpose is to carry a
 * deliberately wrong value — hence the widening of the value type here.
 */
async function fillAndSubmit(overrides: Partial<Record<keyof typeof FORM, string>> = {}) {
  const user = userEvent.setup()
  const values = { ...FORM, ...overrides }

  await user.type(await screen.findByRole('textbox', { name: 'Organization' }), values.organization)
  await user.type(screen.getByRole('textbox', { name: 'Your name' }), values.name)
  await user.type(screen.getByRole('textbox', { name: 'Work email' }), values.email)
  // The whole label, asterisk and all. `/Password/` would address three
  // controls on this screen: the field, its reveal button ("Show password"),
  // and the requirements list ("Password requirements"), because
  // `getByLabelText` matches an `aria-label` as readily as it matches a
  // `<label>`.
  await user.type(screen.getByLabelText(/^Password\*?$/), values.password)

  await user.click(screen.getByRole('button', { name: 'Create workspace' }))
}

beforeEach(() => {
  vi.clearAllMocks()

  useSessionStore.setState({ status: 'anonymous', user: null, accessToken: null })

  api.register.mockResolvedValue(TOKEN)
  api.fetchCurrentUser.mockResolvedValue(ADMIN)
})

describe('creating a workspace', () => {
  it('sends the tenant name under the name the API expects, then lands the new admin home', async () => {
    const memoryRouter = renderRegister()

    await fillAndSubmit()

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Signed in as Dana Whitfield' })
    ).toBeVisible()

    expect(memoryRouter.state.location.pathname).toBe('/dashboard')

    // `organization_name`, not `organizationName` and not `organization`. The
    // server field is snake case and the client sends the server's spelling
    // everywhere; this assertion is what stops a rename reaching a form that
    // cannot act on the 422 it produces.
    expect(api.register).toHaveBeenCalledTimes(1)
    expect(api.register).toHaveBeenCalledWith({
      organization_name: FORM.organization,
      name: FORM.name,
      email: FORM.email,
      password: FORM.password,
    })
  })

  it('renders a server field error beside the input it names', async () => {
    api.register.mockRejectedValue(
      new ApiError({
        message: 'The request could not be processed.',
        code: ERROR_CODES.VALIDATION_ERROR,
        status: 422,
        fieldErrors: [
          { field: 'organization_name', message: 'An organization with this name already exists.' },
        ],
      })
    )

    renderRegister()

    await fillAndSubmit()

    expect(
      await screen.findByText('An organization with this name already exists.')
    ).toBeInTheDocument()

    // The banner suppresses itself when there are field errors, so the same
    // news is not delivered twice in two places.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(useSessionStore.getState().status).toBe('anonymous')
  })

  it('renders a duplicate-email failure on the email field', async () => {
    api.register.mockRejectedValue(
      new ApiError({
        message: 'A user with this email already exists.',
        code: ERROR_CODES.USER_ALREADY_EXISTS,
        status: 409,
        fieldErrors: [{ field: 'email', message: 'This email is already registered.' }],
      })
    )

    renderRegister()

    await fillAndSubmit()

    expect(await screen.findByText('This email is already registered.')).toBeInTheDocument()
  })

  it('refuses a password below the policy floor without troubling the server', async () => {
    renderRegister()

    await fillAndSubmit({ password: 'short' })

    // `noValidate` switches off submission-time constraint validation, so
    // `minLength` on the input is inert — a five-character password passes
    // straight through the browser, through the form, and onto the wire, then
    // comes back a 422 on a form whose user has no account to complain from.
    // That round trip is the thing the page's own comment claims it avoids, and
    // this pair of assertions is what keeps the claim honest: the message, and
    // the request that was never made.
    expect(await screen.findByText('Use at least 12 characters.')).toBeInTheDocument()
    expect(api.register).not.toHaveBeenCalled()

    // On the field, not in the banner — `AuthAlert` suppresses itself when a
    // message has somewhere better to live.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('refuses a password above the ceiling, which no keystroke could have produced', async () => {
    renderRegister()

    // Written in one go rather than typed, and that is the point. `maxLength`
    // truncates the field while typing — `noValidate` has no bearing on it — so
    // 129 characters is a value this input cannot produce by hand. It is still a
    // value autofill can deposit, and the ceiling is all that stands between
    // that and handing a megabyte to the password hasher.
    fireEvent.change(await screen.findByLabelText(/^Password\*?$/), {
      target: { value: 'a'.repeat(129) },
    })

    await userEvent.setup().click(screen.getByRole('button', { name: 'Create workspace' }))

    expect(await screen.findByText('Use no more than 128 characters.')).toBeInTheDocument()
    expect(api.register).not.toHaveBeenCalled()
  })

  it('releases its own refusal once the password clears the floor', async () => {
    api.register.mockRejectedValue(
      new ApiError({
        message: 'The request could not be processed.',
        code: ERROR_CODES.VALIDATION_ERROR,
        status: 422,
        fieldErrors: [{ field: 'email', message: 'This email is already registered.' }],
      })
    )

    renderRegister()

    await fillAndSubmit({ password: 'short' })

    expect(await screen.findByText('Use at least 12 characters.')).toBeInTheDocument()

    const user = userEvent.setup()
    const passwordField = screen.getByLabelText(/^Password\*?$/)

    await user.clear(passwordField)
    await user.type(passwordField, FORM.password)
    await user.click(screen.getByRole('button', { name: 'Create workspace' }))

    // The server is doing the talking now, and its message has replaced the one
    // this form made. The assertion is on the exact string rather than
    // `/at least 12 characters/i`, because the checklist below the field says
    // almost the same thing in a state the user can see — and unlike the old
    // hint, it does not come and go with the error, so a loose match would read
    // it forever and report a release that had not happened.
    expect(await screen.findByText('This email is already registered.')).toBeInTheDocument()
    expect(screen.queryByText('Use at least 12 characters.')).not.toBeInTheDocument()
  })

  /**
   * The checklist and the submit-time refusal read the same two constants, and
   * this is what holds them together: if the floor in `password-policy.ts` ever
   * moves, the tick marks move with it, and the message that follows them still
   * names the same number.
   */
  it('marks the policy satisfied as the password reaches it', async () => {
    renderRegister()

    const requirements = () =>
      screen.getByRole('list', { name: 'Password requirements' })

    expect(requirements()).toHaveTextContent('At least 12 characters, not yet met')

    await userEvent.setup().type(await screen.findByLabelText(/^Password\*?$/), 'twelve-chars')

    expect(requirements()).toHaveTextContent('At least 12 characters, met')
    expect(requirements()).not.toHaveTextContent('At least 12 characters, not yet met')

    // Both rules are stated, not only the one that can fail — a checklist that
    // grows and shrinks as you type is harder to read than one that holds still.
    expect(requirements()).toHaveTextContent('No more than 128 characters, met')
  })
})
