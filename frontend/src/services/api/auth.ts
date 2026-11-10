import { useSessionStore } from '@/store/auth'
import type { components } from '@/types/api'
import type { SessionUser } from '@/types/session'

import { apiClient } from './client'
import { publicClient } from './public-client'
import { refreshSession, type TokenResponse } from './session-refresh'

/**
 * The authentication routes in `backend/app/api/auth.py`.
 *
 * Note which client each one uses, because it is not arbitrary:
 *
 * - `login`, `register`, and password recovery go through `publicClient`.
 *   A wrong password is a 401, and a 401 through `apiClient` would trigger a refresh-and-retry against the
 *   very endpoint that issues credentials.
 * - `logout` and `fetchCurrentUser` go through `apiClient`. Both act on behalf of
 *   a signed-in user, and logout in particular *requires* a valid access token —
 *   a client whose token expired is expected to refresh first, which is exactly
 *   what the interceptor does.
 * - `bootstrapSession` calls `refreshSession`, which uses `publicClient` for the
 *   same reason as login: it is the call that produces a token, not one that
 *   spends one.
 */

export interface LoginPayload {
  email: string
  password: string
}

/**
 * Registration creates an **organization and its first administrator**, not
 * merely a user (`schemas/auth.py`: "There is no way to join an existing
 * organization through this endpoint"). So the form asks for a company name as
 * well as a person's, and the account it produces is necessarily an admin.
 */
export interface RegisterPayload {
  organization_name: string
  name: string
  email: string
  password: string
}

export async function login(payload: LoginPayload): Promise<TokenResponse> {
  const { data } = await publicClient.post<TokenResponse>('/auth/login', payload)

  return data
}

export async function register(payload: RegisterPayload): Promise<components['schemas']['RegistrationResponse']> {
  const { data } = await publicClient.post<components['schemas']['RegistrationResponse']>('/auth/register', payload)

  return data
}

type ForgotPasswordResponse = components['schemas']['ForgotPasswordResponse']

export async function resendVerification(email: string): Promise<void> {
  await publicClient.post('/auth/resend-verification', { email })
}

export async function verifyEmail(token: string): Promise<components['schemas']['VerifyEmailResponse']> {
  const { data } = await publicClient.post<components['schemas']['VerifyEmailResponse']>('/auth/verify-email', { token })
  return data
}
type ResetPasswordPayload = components['schemas']['ResetPasswordRequest']

export async function requestPasswordReset(email: string): Promise<ForgotPasswordResponse> {
  const { data } = await publicClient.post<ForgotPasswordResponse>('/auth/forgot-password', { email })
  return data
}

export async function resetPassword(payload: ResetPasswordPayload): Promise<void> {
  await publicClient.post('/auth/reset-password', payload)
  useSessionStore.getState().endSession()
}

/**
 * End the session, server-side and locally.
 *
 * The local clear is in a `finally` rather than after the await, and that is the
 * point: a user who clicks "Sign out" must end up signed out in this browser
 * even when the request fails. Leaving them apparently signed in because the
 * network was down would be the worst possible behaviour for a button whose
 * entire promise is that it ends the session. `/auth/logout` is idempotent
 * server-side, so an already-revoked or unknown cookie succeeds silently.
 */
export async function logout(): Promise<void> {
  try {
    await apiClient.post('/auth/logout')
  } finally {
    useSessionStore.getState().endSession()
  }
}

export async function fetchCurrentUser(): Promise<SessionUser> {
  const { data } = await apiClient.get<SessionUser>('/auth/me')

  return data
}

/**
 * Establish whether there is a session, on a cold load.
 *
 * The access token is memory-only (see `store/auth.ts`), so a reload always
 * starts here: the refresh cookie is the only surviving credential, and it
 * authenticates this call without any input from the app. The server's answer is
 * the answer — the client never guesses from stored state, because there is none
 * to guess from.
 *
 * Returns the user, or `null` for a visitor who is not signed in. Not an error:
 * an anonymous visitor is the ordinary case, and `refreshSession` already
 * distinguishes it from an expired session when deciding whether to say
 * anything.
 */
export async function bootstrapSession(): Promise<SessionUser | null> {
  try {
    const token = await refreshSession()
    const user = await fetchCurrentUser()

    useSessionStore.getState().startSession(token, user)

    return user
  } catch {
    // Either the refresh was refused — no cookie, so nobody is signed in — or
    // the profile call failed. Both leave the same state, and `endSession` is
    // idempotent, so they need no distinguishing.
    useSessionStore.getState().endSession()

    return null
  }
}

/**
 * The app's bootstrap promise, held so it is created once.
 *
 * React's StrictMode invokes mount effects twice in development, and a second
 * `bootstrapSession` would refresh a **second** time. Because the refresh token
 * rotates on every use, that second call invalidates the first response's
 * token — so the profile request belonging to the first run would be made with
 * a token that had already been revoked. It happens to end in the right state,
 * which is what makes it worth latching rather than debugging: the failure it
 * would cause is intermittent and looks like a backend race.
 *
 * The latch is released when the promise settles, so a genuine retry later in
 * the session is not blocked by it.
 */
let bootstrap: Promise<SessionUser | null> | null = null

export function bootstrapSessionOnce(): Promise<SessionUser | null> {
  if (bootstrap === null) {
    bootstrap = bootstrapSession().finally(() => {
      bootstrap = null
    })
  }

  return bootstrap
}
