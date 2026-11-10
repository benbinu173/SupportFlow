import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionStore } from '@/store/auth'
import { useToastStore } from '@/store/toasts'

import { publicClient } from './public-client'
import { refreshSession, resetRefreshState } from './session-refresh'

vi.mock('./public-client', () => ({
  publicClient: { post: vi.fn() },
}))

const post = vi.mocked(publicClient.post)

function tokenResponse(accessToken: string) {
  return { data: { access_token: accessToken, token_type: 'bearer', expires_in: 900 } }
}

function signedOut() {
  useSessionStore.setState({ status: 'anonymous', user: null, accessToken: null })
}

function signedIn() {
  useSessionStore.setState({
    status: 'authenticated',
    accessToken: 'stale-token',
    user: {
      id: 'u1',
      name: 'Ada Lovelace',
      email: 'ada@example.com',
      role: 'agent',
      is_active: true,
      customer_id: null,
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
      email_verified_at: '2026-01-01T00:00:00Z',
    },
  })
}

beforeEach(() => {
  resetRefreshState()
  useToastStore.setState({ toasts: [] })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('refreshSession', () => {
  it('refreshes once for any number of concurrent callers', async () => {
    signedIn()
    post.mockResolvedValue(tokenResponse('fresh-token'))

    // Six dashboard panels, all 401ing at the same moment. This is the shape
    // that makes single-flight necessary rather than merely tidy: the refresh
    // token rotates on use, so six parallel refreshes would invalidate each
    // other and the backend would log five token-reuse events.
    const results = await Promise.all([
      refreshSession(),
      refreshSession(),
      refreshSession(),
      refreshSession(),
      refreshSession(),
      refreshSession(),
    ])

    expect(post).toHaveBeenCalledTimes(1)
    expect(results).toEqual(Array(6).fill('fresh-token'))
  })

  it('starts a new refresh after the previous one has settled', async () => {
    signedIn()
    post.mockResolvedValueOnce(tokenResponse('first')).mockResolvedValueOnce(tokenResponse('second'))

    await expect(refreshSession()).resolves.toBe('first')
    await expect(refreshSession()).resolves.toBe('second')

    // The counterweight to the test above: single-flight must gate *concurrent*
    // calls, not permanently latch. A latch that never released would mean one
    // refresh per page load.
    expect(post).toHaveBeenCalledTimes(2)
  })

  it('stores the new token and keeps the existing user', async () => {
    signedIn()
    post.mockResolvedValue(tokenResponse('fresh-token'))

    await refreshSession()

    const state = useSessionStore.getState()
    expect(state.accessToken).toBe('fresh-token')
    expect(state.user?.name).toBe('Ada Lovelace')
    expect(state.status).toBe('authenticated')
  })

  it('ends the session and says so when a signed-in user is refused', async () => {
    signedIn()
    post.mockRejectedValue(new Error('401'))

    await expect(refreshSession()).rejects.toThrow()

    expect(useSessionStore.getState().status).toBe('anonymous')
    expect(useSessionStore.getState().accessToken).toBeNull()

    const [toast] = useToastStore.getState().toasts
    expect(toast?.title).toBe('Your session expired')
  })

  it('stays silent when a visitor who was never signed in is refused', async () => {
    signedOut()
    post.mockRejectedValue(new Error('401'))

    await expect(refreshSession()).rejects.toThrow()

    expect(useSessionStore.getState().status).toBe('anonymous')

    // The distinction this test exists for. App bootstrap refreshes on every
    // cold load, including for anonymous visitors — telling each of them their
    // session expired would be a lie, and would train them to ignore the toast
    // that matters.
    expect(useToastStore.getState().toasts).toHaveLength(0)
  })

  it('shares one rejection among concurrent callers rather than retrying', async () => {
    signedIn()
    post.mockRejectedValue(new Error('401'))

    const results = await Promise.allSettled([refreshSession(), refreshSession(), refreshSession()])

    expect(results.map((result) => result.status)).toEqual(['rejected', 'rejected', 'rejected'])
    expect(post).toHaveBeenCalledTimes(1)
  })
})
