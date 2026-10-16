import { Link, Outlet } from 'react-router-dom'

import { Logo } from '@/components/layout/Logo'

/**
 * The shell for sign-in and registration.
 *
 * A centred column rather than the product's sidebar shell, because there is no
 * session yet and therefore no navigation to show — an empty sidebar beside a
 * login form is a picture of an app the visitor cannot use.
 *
 * The ambient wash behind the header is a single radial gradient on a
 * `pointer-events-none` layer rather than a blurred element. A large
 * `filter: blur()` on a full-width block is one of the most expensive things a
 * page can paint, and this is the same image for the cost of a gradient fill.
 */
export function AuthLayout() {
  return (
    <div className="relative min-h-dvh bg-canvas">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-[26rem] [background:radial-gradient(70%_100%_at_50%_0%,var(--color-brand-subtle),transparent_72%)]"
      />

      <main className="relative grid min-h-dvh place-items-center px-4 py-12">
        <div className="flex w-full max-w-sm flex-col gap-7">
          <Link
            to="/"
            className="mx-auto flex items-center justify-center rounded-lg transition-opacity duration-150 ease-out-expo hover:opacity-80"
          >
            <Logo />
            <span className="sr-only">SupportFlow home</span>
          </Link>

          <Outlet />
        </div>
      </main>
    </div>
  )
}
