import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { HOME_BY_ROLE } from '@/routes/home'
import { ApiError, toApiError } from '@/services/api/errors'
import { fetchCurrentUser } from '@/services/api/auth'
import type { TokenResponse } from '@/services/api/session-refresh'
import { useSessionStore } from '@/store/auth'

/**
 * Exchange credentials, load the current profile, and navigate to the role's home.
 *
 * Registration waits for email verification before this session flow can begin.
 *
 * `fetchCurrentUser` rather than trusting a user object from the login response:
 * `/auth/login` returns a token and nothing else (`schemas/auth.py` — the
 * response is deliberately just `access_token`, `token_type`, `expires_in`), and
 * inventing a profile client-side would mean the app's idea of the user's role
 * came from anywhere but the database.
 */
export function useAuthenticate() {
  const navigate = useNavigate()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const authenticate = useCallback(
    async (submit: () => Promise<TokenResponse>, returnTo?: string) => {
      setPending(true)
      setError(null)

      try {
        const token = await submit()

        // **Before `/auth/me`, not after.** The request interceptor authorises
        // from the session store rather than from an argument, so a profile
        // fetched by a token that has not been recorded yet is sent with no
        // `Authorization` header at all — and the 401 that comes back looks,
        // to the interceptor, exactly like an expired session, so it spends a
        // refresh and a replay recovering from a mistake this client made. On a
        // cold load the store is `anonymous` by then (the bootstrap found no
        // cookie), so the interceptor does not even take that recovery path: it
        // rethrows, and the server's "Authentication required." lands on a form
        // the user has just filled in correctly.
        useSessionStore.getState().beginSession(token.access_token)

        const user = await fetchCurrentUser()

        // Read back rather than reusing `token.access_token`. If the profile
        // call 401'd and the interceptor refreshed in the middle of it, the
        // store holds the newer of the two tokens; recording the sign-in token
        // again would discard it and leave the app counting down to an expiry
        // that is already closer than it needs to be.
        const current = useSessionStore.getState().accessToken

        useSessionStore.getState().startSession(current ?? token.access_token, user)

        // `returnTo` is the deep link the guard stashed before bouncing the
        // visitor here. Falling back to the role's home rather than `/` because
        // an agent landing on the marketing page after signing in has been
        // signed in and then sent somewhere that is not their work.
        navigate(returnTo ?? HOME_BY_ROLE[user.role], { replace: true })
        return null
      } catch (cause) {
        // Undo the token recorded above. It belongs to a session that never
        // materialised, and leaving it would put a live credential in a store
        // whose next line sets `anonymous` — the precise state `setAccessToken`
        // refuses to create, and one nothing would clear short of a reload. This
        // also covers the ordinary failure, where `submit` itself was refused
        // and no token was ever recorded; `endSession` is idempotent and the
        // truthful answer to "is there a session?" either way.
        useSessionStore.getState().endSession()

        // Rendered on the form rather than thrown onward: an authentication
        // failure is a thing this screen exists to report, and an error
        // boundary would replace the form the user needs to correct.
        const failure = toApiError(cause)
        setError(failure)
        return failure
      } finally {
        setPending(false)
      }
    },
    [navigate],
  )

  return { authenticate, pending, error }
}
