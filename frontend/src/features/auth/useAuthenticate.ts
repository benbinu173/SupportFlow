import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { HOME_BY_ROLE } from '@/routes/navigation'
import { ApiError, toApiError } from '@/services/api/errors'
import { fetchCurrentUser } from '@/services/api/auth'
import type { TokenResponse } from '@/services/api/session-refresh'
import { useSessionStore } from '@/store/auth'

/**
 * The half of signing in that login and registration share.
 *
 * They look like different screens and are the same transaction: call an
 * endpoint that returns a token, learn who the token belongs to, record the
 * session, and go where that role belongs. Only the call in the middle differs,
 * which is why it is the argument.
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
        const user = await fetchCurrentUser()

        useSessionStore.getState().startSession(token.access_token, user)

        // `returnTo` is the deep link the guard stashed before bouncing the
        // visitor here. Falling back to the role's home rather than `/` because
        // an agent landing on the marketing page after signing in has been
        // signed in and then sent somewhere that is not their work.
        navigate(returnTo ?? HOME_BY_ROLE[user.role], { replace: true })
      } catch (cause) {
        // Rendered on the form rather than thrown onward: an authentication
        // failure is a thing this screen exists to report, and an error
        // boundary would replace the form the user needs to correct.
        setError(toApiError(cause))
      } finally {
        setPending(false)
      }
    },
    [navigate],
  )

  return { authenticate, pending, error }
}
