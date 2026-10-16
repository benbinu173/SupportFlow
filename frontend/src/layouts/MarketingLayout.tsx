import { ArrowRight } from '@phosphor-icons/react'
import { Link, Outlet } from 'react-router-dom'

import { Logo } from '@/components/layout/Logo'
import { useSessionStore } from '@/store/auth'
import { HOME_BY_ROLE } from '@/routes/navigation'

/**
 * The public shell: a floating nav, the page, a footer.
 *
 * Two things about the nav are ADR-010 decisions rather than taste.
 *
 * It is a detached pill rather than an edge-to-edge bar. A strip welded to the
 * top of the viewport reads as chrome; a floating capsule reads as an object on
 * the page, which is the whole difference between a template and a product.
 *
 * It is the only place on this surface where `backdrop-blur` is used, and it is
 * legitimate here because the element is `fixed` — the guardrail exists because
 * a blur on scrolling content forces a GPU repaint every frame. On a fixed
 * layer the blurred region is composited once.
 */
export function MarketingLayout() {
  const status = useSessionStore((state) => state.status)
  const role = useSessionStore((state) => state.user?.role)

  // The nav tells the truth about who is looking at it. Someone already signed
  // in should not be offered "Sign in" — they would click it and be redirected
  // straight back, which is a small, pointless broken promise.
  const signedIn = status === 'authenticated' && role !== undefined
  const home = signedIn ? HOME_BY_ROLE[role] : '/login'

  return (
    <div className="relative min-h-dvh bg-canvas">
      <header className="fixed inset-x-0 top-0 z-sticky flex justify-center px-4 pt-4 sm:pt-6">
        <nav
          aria-label="Main"
          className="pointer-events-auto flex w-full max-w-3xl items-center justify-between gap-3 rounded-full border border-border-subtle bg-surface/80 py-2 pr-2 pl-4 shadow-lg backdrop-blur-xl"
        >
          <Link
            to="/"
            className="flex items-center rounded-full transition-opacity duration-150 ease-out-expo hover:opacity-80"
          >
            <Logo />
          </Link>

          <div className="flex items-center gap-1.5">
            {!signedIn && (
              <Link
                to="/login"
                className="rounded-full px-3.5 py-2 text-[0.8125rem] font-medium text-ink-muted transition-colors duration-150 ease-out-expo hover:bg-surface-hover hover:text-ink"
              >
                Sign in
              </Link>
            )}

            <Link
              to={home}
              className="group inline-flex items-center gap-2 rounded-full bg-brand py-1.5 pr-1.5 pl-4 text-[0.8125rem] font-medium text-brand-fg shadow-sm transition-colors duration-150 ease-out-expo hover:bg-brand-hover"
            >
              {signedIn ? 'Go to your desk' : 'Get started'}

              {/* The button-in-button trailing icon. A bare arrow floating after
                  the label is a glyph; an arrow inside its own disc is a
                  control. It also gives the hover somewhere to travel to. */}
              <span
                aria-hidden="true"
                className="grid size-6 place-items-center rounded-full bg-brand-fg/15 transition-transform duration-300 ease-out-expo group-hover:translate-x-0.5"
              >
                <ArrowRight weight="bold" className="size-3.5" />
              </span>
            </Link>
          </div>
        </nav>
      </header>

      <Outlet />

      <footer className="border-t border-border-subtle px-6 py-10">
        <div className="mx-auto flex max-w-5xl flex-col items-center justify-between gap-4 sm:flex-row">
          <Logo />

          <p className="text-[0.8125rem] text-ink-subtle">
            Built for support teams who measure the pause before the reply.
          </p>
        </div>
      </footer>
    </div>
  )
}
