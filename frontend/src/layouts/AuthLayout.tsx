import { Link, Outlet, useLocation } from 'react-router-dom'

import { AuthPanel, type PanelVariant } from '@/components/auth/AuthPanel'
import { Logo } from '@/components/layout/Logo'
import { Enter } from '@/components/motion/Enter'

/**
 * The panel copy follows the route, and the two screens promise different
 * things — one resumes a workspace, the other creates one — so this is a
 * decision rather than a default. Anything that is not registration is sign-in,
 * because those are the only two routes this layout hosts.
 */
function panelFor(pathname: string): PanelVariant {
  return pathname.startsWith('/register') ? 'register' : 'login'
}

/**
 * The shell for sign-in and registration.
 *
 * An editorial split rather than a centred card: the form holds the left
 * column, and an inset `--color-surface-inverse` panel holds the right one. The
 * panel is what makes these two screens look like a product rather than a
 * template, and it earns its width — a sign-in form is four hundred pixels of
 * content, so at desktop sizes there is a whole second column going spare, and
 * filling it with the ticket lifecycle says more about what the account is for
 * than a larger logo would.
 *
 * Below `lg` the panel is not reflowed, it is absent, and the ambient wash
 * takes over. Two columns need about a thousand pixels before neither of them
 * is cramped, and a form squeezed into three hundred is worse than a form alone
 * on a phone. Nothing in the panel is load-bearing, which is what makes that
 * acceptable: it is the same argument as not showing an empty sidebar beside a
 * login form.
 *
 * The wash behind the header is a single radial gradient on a
 * `pointer-events-none` layer rather than a blurred element. A large
 * `filter: blur()` on a full-width block is one of the most expensive things a
 * page can paint, and this is the same image for the cost of a gradient fill.
 * It is `lg:hidden` because the panel supplies the depth from that width up,
 * and two washes at once is a muddy background.
 */
export function AuthLayout() {
  const { pathname } = useLocation()

  return (
    <div className="relative min-h-dvh bg-canvas lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(0,1.08fr)]">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-[26rem] [background:radial-gradient(70%_100%_at_50%_0%,var(--color-brand-subtle),transparent_72%)] lg:hidden"
      />

      {/* `justify-end` at `lg` rather than centred: the column belongs beside
          the panel it is arguing with, not floating in the middle of its own
          half. The whitespace that opens up on the left is the point of the
          split, not a gap to close. */}
      <main className="relative flex min-h-dvh items-center justify-center px-4 py-12 sm:px-6 lg:justify-end lg:px-12 xl:px-20">
        <div className="flex w-full max-w-[25rem] flex-col gap-8">
          <Enter y={8}>
            <Link
              to="/"
              className="inline-flex items-center gap-2 rounded-lg transition-opacity duration-150 ease-out-expo hover:opacity-80"
            >
              <Logo />
              <span className="sr-only">SupportFlow home</span>
            </Link>
          </Enter>

          <Outlet />
        </div>
      </main>

      <AuthPanel variant={panelFor(pathname)} className="lg:m-3" />
    </div>
  )
}
