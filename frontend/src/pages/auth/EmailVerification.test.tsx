import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, configure, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { router } from '@/routes/router'
import { ApiError, ERROR_CODES } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import type { SessionUser } from '@/types/session'

configure({ asyncUtilTimeout: 10_000 })
const api = vi.hoisted(() => ({ post: vi.fn(), privatePost: vi.fn() }))
vi.mock('@/services/api/public-client', () => ({ publicClient: { post: api.post } }))
vi.mock('@/services/api/client', () => ({ apiClient: { post: api.privatePost } }))
const TOKEN = 'test-email-verification-credential'
const EMAIL = 'owner@example.test'
const USER: SessionUser = {
  id: '00000000-0000-4000-8000-000000000001', name: 'Alex Chen',
  email: 'another@example.test', role: 'agent', is_active: true, customer_id: null,
  created_at: '2026-11-10T00:00:00Z', last_login_at: null, email_verified_at: '2026-11-10T00:00:00Z',
}

beforeAll(async () => {
  await Promise.all([import('@/layouts/AuthLayout'), import('@/pages/auth/VerifyEmailPage'), import('@/pages/auth/LoginPage')])
}, 30_000)

beforeEach(() => {
  vi.clearAllMocks()
  api.post.mockReset()
  api.privatePost.mockReset()
  useSessionStore.getState().endSession()
  localStorage.clear()
  sessionStorage.clear()
})

function open(path = `/verify-email#token=${TOKEN}`) {
  const memoryRouter = createMemoryRouter(router.routes, { initialEntries: [path] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RouterProvider router={memoryRouter} /></QueryClientProvider>)
  return { memoryRouter, client }
}

describe('email verification', () => {
  it('picks up an incoming email fragment while the waiting page is already mounted', async () => {
    api.post.mockResolvedValue({ data: { email: EMAIL, organization_slug: 'workspace' } })
    const { memoryRouter } = open('/verify-email')
    await screen.findByRole('button', { name: 'Send verification link' })
    await act(async () => { await memoryRouter.navigate(`/verify-email#token=${TOKEN}`) })
    await userEvent.click(await screen.findByRole('button', { name: 'Verify email' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Email verified' })).toBeVisible()
    expect(api.post).toHaveBeenCalledExactlyOnceWith('/auth/verify-email', { token: TOKEN })
    expect(memoryRouter.state.location.hash).toBe('')
  })

  it('ignores an older confirmation response after another email link arrives', async () => {
    let finish!: (value: unknown) => void
    api.post.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const { memoryRouter } = open()
    await userEvent.click(await screen.findByRole('button', { name: 'Verify email' }))
    await act(async () => { await memoryRouter.navigate('/verify-email#token=new-link') })
    await act(async () => { finish({ data: { email: 'old@example.test', organization_slug: 'old' } }) })
    expect(screen.queryByRole('heading', { level: 1, name: 'Email verified' })).not.toBeInTheDocument()
    api.post.mockResolvedValue({ data: { email: EMAIL, organization_slug: 'new' } })
    await userEvent.click(screen.getByRole('button', { name: 'Verify email' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Email verified' })).toBeVisible()
    expect(api.post).toHaveBeenLastCalledWith('/auth/verify-email', { token: 'new-link' })
  })

  it('is reachable from sign in without a password', async () => {
    open('/login')
    await userEvent.click(await screen.findByRole('link', { name: 'Resend link' }))
    expect(await screen.findByRole('button', { name: 'Send verification link' })).toBeVisible()
  })

  it('removes the fragment and waits for an explicit confirmation', async () => {
    const { memoryRouter } = open()
    expect(await screen.findByRole('button', { name: 'Verify email' })).toBeVisible()
    await waitFor(() => expect(memoryRouter.state.location.hash).toBe(''))
    expect(api.post).not.toHaveBeenCalled()
    expect(JSON.stringify(localStorage)).not.toContain(TOKEN)
    expect(JSON.stringify(sessionStorage)).not.toContain(TOKEN)
  })

  it('verifies once, then carries the correct workspace to a fresh sign-in', async () => {
    api.post.mockResolvedValue({ data: { email: EMAIL, organization_slug: 'intended-workspace' } })
    const { memoryRouter } = open()
    await userEvent.click(await screen.findByRole('button', { name: 'Verify email' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Email verified' })).toBeVisible()
    expect(api.post).toHaveBeenCalledExactlyOnceWith('/auth/verify-email', { token: TOKEN })
    expect(useSessionStore.getState().status).toBe('anonymous')
    await userEvent.click(screen.getByRole('button', { name: 'Continue to sign in' }))
    expect(await screen.findByRole('textbox', { name: 'Email' })).toHaveValue(EMAIL)
    expect(memoryRouter.state.location.state).toMatchObject({ organizationSlug: 'intended-workspace' })
    expect(memoryRouter.state.location.hash).toBe('')
  })

  it('preserves another session until the user explicitly switches to sign in', async () => {
    useSessionStore.getState().startSession('existing-session', USER)
    api.post.mockResolvedValue({ data: { email: EMAIL, organization_slug: 'other-workspace' } })
    api.privatePost.mockResolvedValue({ data: {} })
    const { client } = open()
    client.setQueryData(['private-data'], { ticket: 'keep while verifying' })
    await userEvent.click(await screen.findByRole('button', { name: 'Verify email' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Email verified' })).toBeVisible()
    expect(useSessionStore.getState().user?.id).toBe(USER.id)
    expect(client.getQueryData(['private-data'])).toBeDefined()
    expect(api.privatePost).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Continue to sign in' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
    expect(api.privatePost).toHaveBeenCalledWith('/auth/logout')
    expect(useSessionStore.getState().status).toBe('anonymous')
    expect(client.getQueryData(['private-data'])).toBeUndefined()
  })

  it('offers resend after a used or expired link without repeating verification', async () => {
    api.post.mockRejectedValue(new ApiError({ code: ERROR_CODES.INVALID_VERIFICATION_TOKEN, status: 400, message: 'Invalid verification link.' }))
    open()
    await userEvent.click(await screen.findByRole('button', { name: 'Verify email' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Request a new verification link' })).toBeVisible()
    expect(screen.getByRole('alert')).toHaveTextContent(/already been used/)
    api.post.mockResolvedValue({ data: {} })
    await userEvent.type(screen.getByRole('textbox', { name: 'Email' }), EMAIL)
    await userEvent.click(screen.getByRole('button', { name: 'Send verification link' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Check your email' })).toBeVisible()
    expect(api.post).toHaveBeenLastCalledWith('/auth/resend-verification', { email: EMAIL })
  })

  it('gives a conditional confirmation for a requested address', async () => {
    api.post.mockResolvedValue({ data: {} })
    open('/verify-email')
    await userEvent.type(await screen.findByRole('textbox', { name: 'Email' }), EMAIL)
    await userEvent.click(screen.getByRole('button', { name: 'Send verification link' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Check your email' })).toBeVisible()
    expect(screen.getByText(/If an active account still needs verification/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Resend verification email' })).toBeVisible()
  })

  it('allows confirmation to be retried after a network failure', async () => {
    api.post.mockRejectedValueOnce(new ApiError({ code: ERROR_CODES.NETWORK_ERROR, status: null, message: 'Connection failed. Try again.' }))
    api.post.mockResolvedValueOnce({ data: { email: EMAIL, organization_slug: 'workspace' } })
    open()
    await userEvent.click(await screen.findByRole('button', { name: 'Verify email' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Connection failed')
    await userEvent.click(screen.getByRole('button', { name: 'Verify email' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Email verified' })).toBeVisible()
    expect(api.post.mock.calls).toEqual([['/auth/verify-email', { token: TOKEN }], ['/auth/verify-email', { token: TOKEN }]])
  })

  it('takes correctly authenticated but unverified users to resend with their email', async () => {
    api.post.mockRejectedValue(new ApiError({ code: ERROR_CODES.EMAIL_NOT_VERIFIED, status: 403, message: 'Verify your email before signing in.' }))
    open('/login')
    await userEvent.type(await screen.findByRole('textbox', { name: 'Email' }), EMAIL)
    await userEvent.type(screen.getByLabelText(/^Password\s*\*?$/), 'a-workspace-password')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('button', { name: 'Send verification link' })).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Email' })).toHaveValue(EMAIL)
    expect(useSessionStore.getState().status).toBe('anonymous')
  })
})
