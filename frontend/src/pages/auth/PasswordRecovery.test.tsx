import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configure, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { router } from '@/routes/router'
import { ApiError, ERROR_CODES } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import type { SessionUser } from '@/types/session'

configure({ asyncUtilTimeout: 10_000 })
const api = vi.hoisted(() => ({ post: vi.fn() }))
vi.mock('@/services/api/public-client', () => ({ publicClient: api }))

const TOKEN = 'test-email-recovery-token'
const PASSWORD = 'a-long-workspace-password'
const USER: SessionUser = {
  id: '00000000-0000-4000-8000-000000000001',
  name: 'Alex Chen', email: 'alex@example.test', role: 'agent', is_active: true,
  customer_id: null, created_at: '2026-11-10T00:00:00Z', last_login_at: null,
}

beforeAll(async () => {
  await Promise.all([
    import('@/layouts/AuthLayout'), import('@/pages/auth/LoginPage'),
    import('@/pages/auth/ForgotPasswordPage'), import('@/pages/auth/ResetPasswordPage'),
  ])
}, 30_000)

beforeEach(() => {
  vi.clearAllMocks()
  api.post.mockReset()
  useSessionStore.getState().endSession()
  localStorage.clear()
  sessionStorage.clear()
})

function open(path = `/reset-password#token=${TOKEN}`) {
  const memoryRouter = createMemoryRouter(router.routes, { initialEntries: [path] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RouterProvider router={memoryRouter} /></QueryClientProvider>)
  return { memoryRouter, client }
}

async function fillPasswords(password = PASSWORD, confirmation = password) {
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText(/^New password/, { selector: 'input' }), password)
  await user.type(screen.getByLabelText(/^Confirm password/, { selector: 'input' }), confirmation)
  await user.click(screen.getByRole('button', { name: 'Update password' }))
}

describe('password recovery', () => {
  it('is reachable from sign in', async () => {
    open('/login')
    await userEvent.click(await screen.findByRole('link', { name: 'Forgot password?' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Forgot your password?' })).toBeVisible()
  })

  it('requests a link and gives a conditional email confirmation', async () => {
    api.post.mockResolvedValue({ data: { detail: 'If an active account matches...' } })
    open('/forgot-password')
    await userEvent.type(await screen.findByRole('textbox', { name: 'Email' }), USER.email)
    await userEvent.click(screen.getByRole('button', { name: 'Send reset link' }))
    expect(api.post).toHaveBeenCalledWith('/auth/forgot-password', { email: USER.email })
    expect(await screen.findByRole('heading', { level: 1, name: 'Check your email' })).toBeVisible()
    expect(screen.getByText(/If an active account matches/)).toBeVisible()
  })

  it('shows the retry time when an email request is limited', async () => {
    api.post.mockRejectedValue(new ApiError({ code: ERROR_CODES.RATE_LIMITED, status: 429, message: 'Limited', retryAfterSeconds: 42 }))
    open('/forgot-password')
    await userEvent.type(await screen.findByRole('textbox', { name: 'Email' }), USER.email)
    await userEvent.click(screen.getByRole('button', { name: 'Send reset link' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Try again in 42 seconds.')
  })

  it('keeps request validation errors beside the email input', async () => {
    api.post.mockRejectedValue(new ApiError({ code: ERROR_CODES.VALIDATION_ERROR, status: 422, message: 'Invalid request', fieldErrors: [{ field: 'email', message: 'Enter a valid email address.' }] }))
    open('/forgot-password')
    await userEvent.type(await screen.findByRole('textbox', { name: 'Email' }), 'invalid')
    await userEvent.click(screen.getByRole('button', { name: 'Send reset link' }))
    expect(await screen.findByText('Enter a valid email address.')).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Email' })).toHaveAttribute('aria-invalid', 'true')
  })

  it('reads the email fragment once and removes it from the address', async () => {
    const { memoryRouter } = open()
    expect(await screen.findByRole('heading', { level: 1, name: 'Set a new password' })).toBeVisible()
    await waitFor(() => expect(memoryRouter.state.location.hash).toBe(''))
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('rejects a short password before making a request', async () => {
    open()
    await fillPasswords('short')
    expect(screen.getByText('Use at least 12 characters.')).toBeVisible()
    expect(api.post).not.toHaveBeenCalled()
  })

  it('requires matching confirmation before making a request', async () => {
    open()
    await fillPasswords(PASSWORD, 'a-different-long-password')
    expect(screen.getByText("Passwords don't match.")).toBeVisible()
    expect(api.post).not.toHaveBeenCalled()
  })

  it('ends the current browser session and clears private cached data after success', async () => {
    api.post.mockResolvedValue({ data: { detail: 'Password updated.' } })
    useSessionStore.getState().startSession('old-access-token', USER)
    const { client } = open()
    client.setQueryData(['private-tickets'], ['old-private-data'])
    await fillPasswords()
    expect(api.post).toHaveBeenCalledWith('/auth/reset-password', { token: TOKEN, password: PASSWORD })
    expect(await screen.findByRole('heading', { level: 1, name: 'Password updated' })).toBeVisible()
    expect(useSessionStore.getState().status).toBe('anonymous')
    expect(useSessionStore.getState().accessToken).toBeNull()
    expect(client.getQueryData(['private-tickets'])).toBeUndefined()
    expect(screen.queryByLabelText(/^New password/, { selector: 'input' })).toBeNull()
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login')
  })

  it('offers a fresh link after an expired or used link is refused', async () => {
    api.post.mockRejectedValue(new ApiError({ code: ERROR_CODES.INVALID_RESET_TOKEN, status: 400, message: 'This reset link is invalid or has expired. Request a new one.' }))
    open()
    await fillPasswords()
    expect(await screen.findByRole('heading', { level: 1, name: 'Request a new reset link' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Request reset link' })).toHaveAttribute('href', '/forgot-password')
    expect(screen.getByRole('alert')).toHaveTextContent('invalid or has expired')
  })

  it('explains a missing link without submitting anything', async () => {
    open('/reset-password')
    expect(await screen.findByRole('heading', { level: 1, name: 'Request a new reset link' })).toBeVisible()
    expect(api.post).not.toHaveBeenCalled()
  })
})
