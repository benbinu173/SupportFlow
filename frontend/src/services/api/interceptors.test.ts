import axios, { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionStore } from '@/store/auth'

import { installInterceptors } from './interceptors'
import { publicClient } from './public-client'
import { resetRefreshState } from './session-refresh'

vi.mock('./public-client', () => ({
  publicClient: { post: vi.fn() },
}))

const refreshPost = vi.mocked(publicClient.post)

interface RecordedCall {
  url: string
  authorization: string | undefined
}

/**
 * An axios instance whose transport is a function.
 *
 * Using a real adapter rather than mocking axios means the interceptors run in
 * the position they actually run in — request interceptors, then the adapter,
 * then response interceptors — so "retried exactly once" is measured against a
 * genuine round trip and not against a stub that never had the chance to loop.
 */
function createInstance(status: number) {
  const calls: RecordedCall[] = []

  const instance = axios.create({
    baseURL: '/api/v1',
    adapter: (config: InternalAxiosRequestConfig) => {
      const authorization = config.headers.get('Authorization')

      calls.push({
        url: config.url ?? '',
        authorization: typeof authorization === 'string' ? authorization : undefined,
      })

      if (status >= 400) {
        return Promise.reject(
          new AxiosError('Request failed', 'ERR_BAD_REQUEST', config, null, {
            status,
            statusText: '',
            headers: new AxiosHeaders(),
            data: {
              error: { code: 'AUTHENTICATION_REQUIRED', message: 'Authentication required.' },
            },
            config,
          }),
        )
      }

      return Promise.resolve({
        status,
        statusText: 'OK',
        headers: new AxiosHeaders(),
        data: { ok: true },
        config,
      })
    },
  })

  installInterceptors(instance)

  return { instance, calls }
}

function signedIn(token = 'stale-token') {
  useSessionStore.setState({
    status: 'authenticated',
    accessToken: token,
    user: {
      id: 'u1',
      name: 'Ada Lovelace',
      email: 'ada@example.com',
      role: 'agent',
      is_active: true,
      customer_id: null,
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    },
  })
}

function signedOut() {
  useSessionStore.setState({ status: 'anonymous', user: null, accessToken: null })
}

beforeEach(() => {
  resetRefreshState()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('auth interceptors', () => {
  it('attaches the access token to outgoing requests', async () => {
    signedIn('live-token')
    const { instance, calls } = createInstance(200)

    await instance.get('/tickets')

    expect(calls).toEqual([{ url: '/tickets', authorization: 'Bearer live-token' }])
  })

  it('refreshes on a 401 and replays the request with the new token', async () => {
    signedIn('stale-token')
    refreshPost.mockResolvedValue({
      data: { access_token: 'fresh-token', token_type: 'bearer', expires_in: 900 },
    })

    // Fails the first time and succeeds after, which is the real sequence: the
    // token was stale, the refresh fixed it, the replay works.
    const calls: RecordedCall[] = []
    let attempt = 0

    const instance = axios.create({
      baseURL: '/api/v1',
      adapter: (config: InternalAxiosRequestConfig) => {
        attempt += 1
        const authorization = config.headers.get('Authorization')
        calls.push({
          url: config.url ?? '',
          authorization: typeof authorization === 'string' ? authorization : undefined,
        })

        if (attempt === 1) {
          return Promise.reject(
            new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, null, {
              status: 401,
              statusText: '',
              headers: new AxiosHeaders(),
              data: { error: { code: 'AUTHENTICATION_REQUIRED', message: 'Authentication required.' } },
              config,
            }),
          )
        }

        return Promise.resolve({
          status: 200,
          statusText: 'OK',
          headers: new AxiosHeaders(),
          data: { ok: true },
          config,
        })
      },
    })

    installInterceptors(instance)

    const response = await instance.get('/tickets')

    expect(response.status).toBe(200)
    expect(refreshPost).toHaveBeenCalledTimes(1)
    expect(calls).toEqual([
      { url: '/tickets', authorization: 'Bearer stale-token' },
      { url: '/tickets', authorization: 'Bearer fresh-token' },
    ])
  })

  it('retries at most once when the server keeps refusing', async () => {
    signedIn()
    refreshPost.mockResolvedValue({
      data: { access_token: 'fresh-token', token_type: 'bearer', expires_in: 900 },
    })

    const { instance, calls } = createInstance(401)

    await expect(instance.get('/tickets')).rejects.toThrow()

    // The whole point of the latch. Without it this is an infinite loop, and it
    // is reached by any cause refreshing cannot fix — a deactivated account, a
    // clock skew that makes every freshly-minted token look expired. Two calls:
    // the original and one replay.
    expect(calls).toHaveLength(2)
    expect(refreshPost).toHaveBeenCalledTimes(1)
  })

  it('does not refresh when no one is signed in', async () => {
    signedOut()
    const { instance, calls } = createInstance(401)

    await expect(instance.get('/tickets')).rejects.toThrow()

    expect(refreshPost).not.toHaveBeenCalled()
    expect(calls).toHaveLength(1)
  })

  it('does not refresh on a 403', async () => {
    signedIn()
    const { instance, calls } = createInstance(403)

    await expect(instance.get('/tickets')).rejects.toThrow()

    // A 403 is a valid identity without the rights, so the token is fine.
    // Refreshing here would rotate the refresh token on every forbidden request.
    expect(refreshPost).not.toHaveBeenCalled()
    expect(calls).toHaveLength(1)
  })

  it('surfaces the original 401 when the refresh also fails', async () => {
    signedIn()
    refreshPost.mockRejectedValue(new Error('refresh refused'))

    const { instance, calls } = createInstance(401)

    await expect(instance.get('/tickets')).rejects.toMatchObject({
      response: { status: 401 },
    })

    // Callers asked for a ticket and were refused because identity failed. The
    // reason the *renewal* failed is already a cleared session and a toast;
    // replacing this error with that one would report a single user-visible
    // event as two different failures.
    expect(calls).toHaveLength(1)
    expect(useSessionStore.getState().status).toBe('anonymous')
  })
})
