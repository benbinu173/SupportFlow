import { useSessionStore } from '@/store/auth'
import { toast } from '@/store/toasts'

import { toApiError } from './errors'
import { publicClient } from './public-client'

/**
 * `TokenResponse` from `backend/app/schemas/auth.py`.
 *
 * The refresh token is not a field, and its absence is the design: it is an
 * `HttpOnly` cookie the browser sends without being asked, so there is nothing
 * here for this code to store and nothing for a script to steal (ADR-014).
 */
export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
}

/**
 * The in-flight refresh, or `null`.
 *
 * This variable is the whole of the single-flight behaviour, and it exists
 * because of a specific failure. A page that loads and immediately fires six
 * parallel requests — a dashboard's every panel — does so with a token that may
 * already have expired. All six come back 401 at once. Without this, all six
 * call `/auth/refresh`; the refresh token **rotates on every use** (ADR-003), so
 * the first call invalidates the token the other five are holding, five of them
 * fail, and the backend records five token-reuse events that look exactly like a
 * stolen credential being replayed. One refresh, five retries.
 */
let inFlight: Promise<string> | null = null

/**
 * Obtain a fresh access token, refreshing at most once for any number of
 * concurrent callers.
 *
 * Failing also ends the session and says so — but only when there was a session
 * to lose. See `runRefresh`.
 */
export function refreshSession(): Promise<string> {
  if (inFlight === null) {
    inFlight = runRefresh().finally(() => {
      // Cleared before the awaiting callers resume, so a request that 401s
      // immediately after a failed refresh starts a clean attempt rather than
      // joining one that has already settled.
      inFlight = null
    })
  }

  return inFlight
}

async function runRefresh(): Promise<string> {
  // Captured before the request, because a failure is about to change it. This
  // is the difference between "your session expired, sign in again" and a
  // visitor who was never signed in getting told their session expired.
  const hadSession = useSessionStore.getState().status === 'authenticated'

  try {
    // Through `publicClient`, not `apiClient`: this request must not be
    // intercepted, or a 401 here would trigger the refresh it is already
    // performing.
    const { data } = await publicClient.post<TokenResponse>('/auth/refresh')

    useSessionStore.getState().setAccessToken(data.access_token)

    return data.access_token
  } catch (cause) {
    useSessionStore.getState().endSession()

    if (hadSession) {
      toast.error('Your session expired', 'Sign in again to pick up where you left off.')
    }

    throw toApiError(cause)
  }
}

/** Test seam. Resets the single-flight latch so one test cannot leak into the next. */
export function resetRefreshState(): void {
  inFlight = null
}
