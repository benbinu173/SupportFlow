import { useEffect, type ReactNode } from 'react'

import { bootstrapSessionOnce } from '@/services/api/auth'

/**
 * Asks the server who the visitor is, once, on mount.
 *
 * This is the only thing that turns `status: 'unknown'` into a real answer. The
 * guards deliberately render a pending state until it resolves rather than
 * assuming `anonymous`, so without this the whole app sits on a spinner — the
 * failure is loud rather than silent, which is the reason the state exists.
 *
 * The request itself is latched inside `bootstrapSessionOnce`, so StrictMode's
 * double effect cannot refresh twice.
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  useEffect(() => {
    // A rejection here is already handled: `bootstrapSession` resolves to `null`
    // for a visitor who is not signed in, and ends the session for one whose
    // cookie was refused. The `void` is what says the promise is deliberately
    // not being awaited rather than overlooked.
    void bootstrapSessionOnce()
  }, [])

  return <>{children}</>
}
