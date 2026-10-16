import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { ErrorState } from '@/components/ui/States'
import { Spinner } from '@/components/ui/Spinner'
import { can, type Permission, type UserRole } from '@/features/auth/permissions'
import { useSessionStore } from '@/store/auth'

/**
 * Route guards.
 *
 * **A guard here is a usability affordance and never a control.** This is the
 * same statement `features/auth/permissions.ts` makes about the matrix it
 * mirrors, and it is worth repeating at the place someone might mistake it for
 * enforcement: every route re-evaluates permissions server-side against the
 * caller's verified token, so a user who edits the store in a console gains
 * exactly one thing — the chance to look at a screen whose data requests will
 * all come back 403. What these buy is that a customer never sees an Assign
 * button, and never waits for a request to tell them they cannot.
 */

/**
 * The moment between page load and the bootstrap response.
 *
 * Not a spinner in the middle of a blank page: this is the whole viewport while
 * the app genuinely does not know whether the visitor is signed in. Rendered
 * rather than redirected, because redirecting on `unknown` is what sends a
 * signed-in user to `/login` for a frame and then bounces them back.
 */
function PendingSession() {
  return (
    <main className="grid min-h-dvh place-items-center bg-canvas">
      <Spinner size="lg" label="Checking your session" />
    </main>
  )
}

/**
 * Requires a session, and remembers where the user was going.
 *
 * The `state.from` is what makes a deep link survivable: someone who opens a
 * ticket URL, gets bounced to sign in, and lands back on their dashboard has
 * lost the thing they clicked. The login page reads this and returns them.
 */
export function RequireAuth() {
  const status = useSessionStore((state) => state.status)
  const location = useLocation()

  if (status === 'unknown') {
    return <PendingSession />
  }

  if (status === 'anonymous') {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }

  return <Outlet />
}

/**
 * Requires one capability, and offers a way onward rather than a way back.
 *
 * A refusal renders where the screen would have been, inside the shell, instead
 * of redirecting. A redirect to the dashboard would be a silent failure — the
 * user clicks something, the URL changes to a page they did not ask for, and
 * nothing explains why. This says what happened.
 */
export function RequirePermission({ permission }: { permission: Permission }) {
  const role = useSessionStore((state) => state.user?.role)

  if (!can(role, permission)) {
    return (
      <ErrorState
        title="You do not have access to this page"
        description="Your role does not include this area. If you think that is wrong, ask an administrator to check your permissions."
      />
    )
  }

  return <Outlet />
}

/**
 * Sends an already-signed-in visitor away from the auth screens.
 *
 * Landing on `/login` while holding a valid session is a confusing dead end —
 * the form would either fail or, worse, succeed and replace the session. Used by
 * the login and registration routes only.
 */
export function RedirectIfAuthenticated({ to }: { to: (role: UserRole) => string }) {
  const status = useSessionStore((state) => state.status)
  const role = useSessionStore((state) => state.user?.role)

  if (status === 'unknown') {
    return <PendingSession />
  }

  if (status === 'authenticated' && role) {
    return <Navigate to={to(role)} replace />
  }

  return <Outlet />
}
