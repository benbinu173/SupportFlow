import { create } from 'zustand'

import type { SessionUser } from '@/types/session'

/**
 * The session, as this browser tab holds it.
 *
 * **The access token lives in memory and nowhere else.** It is not in
 * `localStorage` and not in `sessionStorage`, and that is the whole point of the
 * backend's cookie design: the refresh token is `HttpOnly` so that a
 * cross-site-scripting flaw cannot read a long-lived credential, and copying the
 * access token into JS-readable storage would hand that same flaw a shorter-lived
 * one for no benefit. A reload loses the token and costs one `/auth/refresh`,
 * which the cookie authenticates silently — a round trip on page load is a much
 * better trade than a credential in `localStorage` for the tab's lifetime.
 *
 * So: this store is *app* state, but it is the kind of app state that has a
 * deliberate lifetime. `persist` middleware is not used here on purpose.
 */

export type SessionStatus =
  /** No bootstrap has run yet. Distinct from `anonymous` — see below. */
  | 'unknown'
  | 'authenticated'
  | 'anonymous'

interface SessionState {
  status: SessionStatus
  /** `null` unless `status` is `authenticated`. */
  user: SessionUser | null
  /** `null` unless `status` is `authenticated`. */
  accessToken: string | null

  startSession: (accessToken: string, user: SessionUser) => void
  /** Record a token from an explicit sign-in, before the profile is fetched. */
  beginSession: (accessToken: string) => void
  /** Refresh the profile without touching the token — after a role change, say. */
  replaceUser: (user: SessionUser) => void
  /** Store a token obtained from `/auth/refresh`, keeping the user. */
  setAccessToken: (accessToken: string) => void
  endSession: () => void
}

/**
 * `unknown` is load-bearing and is not the same as `anonymous`.
 *
 * On a hard reload the store starts empty, but whether the visitor is signed in
 * is not yet known — the refresh cookie may well be sitting there. Collapsing
 * the two into `anonymous` would make the guards redirect a signed-in user to
 * `/login` for the moment between mount and the bootstrap response, and then
 * bounce them back. `unknown` is what lets a route render a pending state
 * instead of guessing.
 *
 * `anonymous` is only ever reached after the server has actually said no.
 */
const INITIAL_STATUS: SessionStatus = 'unknown'

export const useSessionStore = create<SessionState>()((set) => ({
  status: INITIAL_STATUS,
  user: null,
  accessToken: null,

  startSession: (accessToken, user) => {
    set({ status: 'authenticated', accessToken, user })
  },

  /**
   * The token a sign-in just produced, recorded before its profile is fetched.
   *
   * **This is a separate step for one blunt reason: the request interceptor
   * authorises from this store, not from an argument.** `/auth/me` therefore
   * goes out with no `Authorization` header at all unless the token is already
   * here — and a 401 to the interceptor is indistinguishable from an expired
   * session, so it spends a refresh and a replay recovering from an error this
   * client made. `bootstrapSession` avoids the trap by accident: `refreshSession`
   * records the token as a side effect before it returns, so the profile call
   * that follows it is authorised. Nothing in `login()` does the equivalent,
   * which is why signing in was the one path that failed.
   *
   * **It does not touch `status`, and that is deliberate — not an oversight.**
   * The tempting move is to mark `unknown`, since a credential now exists but
   * the user behind it is still unconfirmed. That would strand people:
   * `RedirectIfAuthenticated` renders a waiting state for `unknown` instead of
   * its children, so a sign-in that succeeded against the server but whose
   * profile call then failed would replace the login form — and the error
   * explaining what happened — with a spinner that never resolves. Leaving the
   * status alone keeps the form mounted and the failure reportable.
   *
   * Unlike `setAccessToken` below this is unguarded, because the two record
   * tokens from different directions. That one takes a renewal the browser
   * obtained on its own from a cookie, so one arriving after a sign-out must be
   * dropped. This one takes the answer to a form the user just submitted —
   * the most authoritative thing this store will ever hear — and it has to
   * outrank an `anonymous` left behind by a bootstrap that ran before they had
   * an account to sign in with.
   */
  beginSession: (accessToken) => {
    set({ accessToken })
  },

  replaceUser: (user) => {
    // Guarded rather than unconditional: a profile response that lands after a
    // sign-out must not resurrect an authenticated status with no token behind
    // it. The request was in flight when the session ended; it loses.
    if (useSessionStore.getState().status !== 'authenticated') {
      return
    }

    set({ user })
  },

  setAccessToken: (accessToken) => {
    // Allowed while `unknown` as well as while `authenticated`, and that is not
    // a loophole: app bootstrap calls `/auth/refresh` before it knows whether
    // anyone is signed in, and the token it gets back has to land somewhere
    // before `/auth/me` can be called with it.
    //
    // `anonymous` is the one state that refuses. That state is only reached
    // after the server has said no, so a refresh response arriving late — the
    // request was in flight when the user signed out — must not leave a token
    // sitting in a store whose status says there is no session.
    if (useSessionStore.getState().status === 'anonymous') {
      return
    }

    set({ accessToken })
  },

  endSession: () => {
    set({ status: 'anonymous', user: null, accessToken: null })
  },
}))

/**
 * Read the current access token without subscribing.
 *
 * For the axios interceptor, which is not a component and cannot use the hook.
 * `getState` rather than a captured value, so the interceptor always sees the
 * token as it is at request time rather than as it was when it was installed.
 */
export function currentAccessToken(): string | null {
  return useSessionStore.getState().accessToken
}
