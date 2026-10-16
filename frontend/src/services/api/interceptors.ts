import type { AxiosError, AxiosInstance, InternalAxiosRequestConfig } from 'axios'

import { currentAccessToken, useSessionStore } from '@/store/auth'

import { refreshSession } from './session-refresh'

/**
 * A request config that remembers whether it has already been replayed.
 *
 * Prefixed because it is our field riding on an axios object, so it is obvious
 * at a glance which properties here are the library's and which are ours.
 */
interface RetriableConfig extends InternalAxiosRequestConfig {
  _retried?: boolean
}

/**
 * Attach the access token, and recover from its expiry.
 *
 * Takes the instance as an argument rather than importing it. That is what keeps
 * this module a leaf: `client.ts` installs these and this file never needs to
 * know that module exists, so there is no import cycle to reason about when the
 * retry below re-issues a request through the same instance.
 */
export function installInterceptors(instance: AxiosInstance): void {
  instance.interceptors.request.use((config) => {
    const token = currentAccessToken()

    if (token) {
      config.headers.set('Authorization', `Bearer ${token}`)
    }

    return config
  })

  instance.interceptors.response.use(
    (response) => response,
    (error: AxiosError) => recoverFromUnauthorized(instance, error),
  )
}

/**
 * On a 401, refresh once and replay the request once.
 *
 * **Once** is doing real work in that sentence. A retry without a latch is an
 * infinite loop the moment the server keeps answering 401 — which it will, if
 * the token is rejected for a reason refreshing cannot fix, such as a
 * deactivated account (the API answers `FORBIDDEN` there) or a clock skew bad
 * enough to make every freshly-minted token look expired. The latch is what
 * turns that from a request storm into one failure.
 */
async function recoverFromUnauthorized(
  instance: AxiosInstance,
  error: AxiosError,
): Promise<never> {
  const config = error.config as RetriableConfig | undefined

  // Anything that is not a 401, or did not carry a config to replay — a network
  // failure has no `config` in some axios paths — is not this function's
  // business. Rethrown unchanged so a caller's own error handling sees it.
  if (!config || error.response?.status !== 401 || config._retried) {
    throw error
  }

  // No session to renew. A 401 while signed out is the correct answer to a
  // request that should not have been made, and refreshing here would be a
  // round trip to confirm what the store already knows.
  if (useSessionStore.getState().status === 'anonymous') {
    throw error
  }

  config._retried = true

  try {
    const token = await refreshSession()

    // `config.headers` is an `AxiosHeaders` by the time a response interceptor
    // sees it, so `.set` rather than assignment — assigning would replace the
    // instance with a plain object and lose the other headers.
    config.headers.set('Authorization', `Bearer ${token}`)

    return await instance.request(config)
  } catch {
    // The *original* 401 is rethrown, not the refresh's error. The caller asked
    // for one thing and it failed because the identity was refused; the reason
    // the renewal did not work is already a toast and a cleared session, and
    // surfacing a second, different error for the same user-visible event would
    // only make one failure report as two.
    throw error
  }
}
