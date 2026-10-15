import { MagnifyingGlass } from '@phosphor-icons/react'
import { Link } from 'react-router-dom'

import { Logo } from '@/components/layout/Logo'
import { buttonClasses } from '@/components/ui/button-classes'

/**
 * The `*` route.
 *
 * Distinct from `RouteError`'s 404 case: this is a URL that matched nothing,
 * which is a navigation outcome rather than a failure, and it should not look
 * like one. No red, no warning triangle, no apology.
 */
export function NotFoundPage() {
  return (
    <main className="grid min-h-dvh place-items-center bg-canvas p-6">
      <div className="flex w-full max-w-md flex-col items-center gap-5 text-center">
        <Logo />

        <div className="flex size-12 items-center justify-center rounded-2xl border border-border-subtle bg-surface-sunken text-ink-subtle [&_svg]:size-6">
          <MagnifyingGlass aria-hidden="true" />
        </div>

        <div className="flex flex-col gap-1.5">
          <h1 className="text-lg font-semibold text-ink">Page not found</h1>
          <p className="text-sm leading-relaxed text-ink-muted">
            Nothing lives at this address. Check the link, or head back to somewhere that exists.
          </p>
        </div>

        <Link to="/" className={buttonClasses({ className: 'mt-1' })}>
          Go home
        </Link>
      </div>
    </main>
  )
}
