import { ArrowClockwise, WarningOctagon } from '@phosphor-icons/react'
import { isRouteErrorResponse, useRouteError } from 'react-router-dom'

import { Logo } from '@/components/layout/Logo'
import { Button } from '@/components/ui/Button'

/**
 * The root error element.
 *
 * React Router renders this instead of the route tree when a render throws, so
 * it must not depend on anything that could itself be the thing that threw —
 * no query client, no auth store, no theme context. Everything here is local.
 *
 * A 404 arrives through the same channel as a crash, and reporting "an
 * unexpected error occurred" for a mistyped URL would be both alarming and
 * untrue, so the two are separated at the top.
 */
export function RouteError() {
  const error = useRouteError()

  const isNotFound = isRouteErrorResponse(error) && error.status === 404

  const detail = isNotFound
    ? 'That page does not exist. It may have been moved, or the link that brought you here may be out of date.'
    : error instanceof Error
      ? error.message
      : typeof error === 'string'
        ? error
        : 'An unexpected error occurred while rendering this page.'

  return (
    <main className="grid min-h-dvh place-items-center bg-canvas p-6">
      <div className="flex w-full max-w-md flex-col items-center gap-5 text-center">
        <Logo />

        <div className="flex size-12 items-center justify-center rounded-2xl border border-danger-border bg-danger-subtle text-danger-text [&_svg]:size-6">
          <WarningOctagon aria-hidden="true" />
        </div>

        <div className="flex flex-col gap-1.5">
          <h1 className="text-lg font-semibold text-ink">
            {isNotFound ? 'Page not found' : 'Something went wrong'}
          </h1>
          <p className="text-sm leading-relaxed text-ink-muted">{detail}</p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            onClick={() => {
              window.location.reload()
            }}
            leadingIcon={<ArrowClockwise />}
          >
            Reload
          </Button>

          {/* A full navigation rather than a router link: if the router is what
              is broken, asking the router to navigate is not a way out. */}
          <Button
            onClick={() => {
              window.location.assign('/')
            }}
          >
            Go home
          </Button>
        </div>

        {/* The stack is shown in development only. It is the single most useful
            thing when this page appears while building, and the single least
            useful thing to ship. */}
        {import.meta.env.DEV && error instanceof Error && error.stack && (
          <pre className="mt-2 max-h-64 w-full overflow-auto rounded-lg border border-border-subtle bg-surface-sunken p-3 text-left text-2xs leading-relaxed text-ink-muted">
            {error.stack}
          </pre>
        )}
      </div>
    </main>
  )
}
