import { Enter } from '@/components/motion/Enter'
import { usePointerParallax } from '@/hooks/usePointerParallax'
import { cn } from '@/utils/cn'

import { AuthPanelCards, AuthPanelGlow, type PanelCard } from './AuthPanelVisual'

export type PanelVariant = 'login' | 'register' | 'recovery' | 'verification'

interface PanelCopy {
  eyebrow: string
  headline: string
  body: string
  footnote: string
  cards: readonly PanelCard[]
}

/**
 * The right-hand pane of the auth screens.
 *
 * Two rules govern every word in here, and they are the landing page's rules
 * because it is the same surface making the same kind of promise:
 *
 * **Nothing describes a feature the product does not have.** The lifecycle,
 * the note confidentiality and the role matrix are all enforced in the server
 * today. A sign-in screen is the worst place to over-claim, because the user is
 * about to find out.
 *
 * **No numbers** (§60). No "10× faster", no "used by 400 teams" — there is
 * nothing behind a figure like that here, and inventing one to fill a panel is
 * exactly the fabrication the spec forbids.
 *
 * The footnote is the one place this differs from marketing copy: it states a
 * security property, so it has to be true in the same way the code is.
 * `sf_refresh` really is set `HttpOnly; SameSite=lax; Path=/api/v1/auth`, and
 * the access token really does live only in the zustand store — which is what
 * `LoginPage.test.tsx` asserts by checking that nothing reaches web storage.
 */
const PANEL_COPY: Record<PanelVariant, PanelCopy> = {
  verification: {
    eyebrow: 'Email verification',
    headline: 'One last step. Then your workspace.',
    body: 'Confirm your email address so you can sign in and get started with your team.',
    footnote: 'Each email names its workspace. Verification links expire and can be used once. Your password stays the same.',
    cards: [
      { kind: 'prose', eyebrow: 'Your address', body: 'Open the link sent to your email and confirm that it belongs to you.' },
      { kind: 'prose', eyebrow: 'Your workspace', body: 'Sign in with your password after verification to start working.' },
    ],
  },
  recovery: {
    eyebrow: 'Account recovery',
    headline: 'Get back to your workspace.',
    body: 'Your tickets and conversations stay right where you left them. A new password gets you back to the work that needs you.',
    footnote: 'Your password changes only after you confirm a new one. If you did not request a reset, you can ignore the email.',
    cards: [
      { kind: 'prose', eyebrow: 'Your workspace', body: 'The email names your workspace. Other passwords stay the same.' },
      { kind: 'prose', eyebrow: 'A private link', body: 'Reset links go to your work email, expire, and can be used once.' },
    ],
  },
  login: {
    eyebrow: 'Welcome back',
    headline: 'Pick up where your queue left off.',
    body: 'Your tickets, the full history behind each one, and the SLA clock that governs it — waiting exactly as you left them.',
    footnote:
      'Sessions use a short-lived access token held in memory. The refresh token is an HttpOnly cookie, so no script on this page can read it.',
    cards: [
      {
        kind: 'chips',
        eyebrow: 'Lifecycle',
        // All six members of `TicketStatus`, in transition order, and named
        // the way the enum names them rather than the way they read best:
        // `waiting_for_customer` is "Waiting for customer" here because that
        // is the term the product uses elsewhere. Nothing in the frontend has
        // needed a display label for these yet — F4 will build that map — so
        // inventing a friendlier synonym now would only guarantee that this
        // panel and the queue disagree about what the state is called.
        chips: ['Open', 'Assigned', 'In progress', 'Waiting for customer', 'Resolved', 'Closed'],
      },
      {
        kind: 'prose',
        eyebrow: 'Internal notes',
        body: 'Staff-only, always — a note is never sent to the customer, and the interface never offers to.',
      },
    ],
  },
  register: {
    eyebrow: 'New workspace',
    headline: 'You are about to become its administrator.',
    body: 'A workspace is created for your organisation and nobody else’s, with you as its first account and every permission in the matrix.',
    footnote:
      'This form creates a new workspace rather than joining one. Everyone you add afterwards belongs to it, and the server scopes every request to it.',
    cards: [
      {
        kind: 'chips',
        eyebrow: 'Roles',
        chips: ['Admin', 'Manager', 'Agent', 'Customer'],
      },
      {
        kind: 'prose',
        eyebrow: 'Where it starts',
        body: 'A ticket queue, a customer directory, and an SLA policy you set per priority.',
      },
    ],
  },
}

/**
 * Rendered beside the form at `lg` and above, and not at all below it.
 *
 * The width is the reason. A two-column arrangement of a form and a panel needs
 * roughly a thousand pixels before either column stops being cramped, and a
 * form squeezed into 300px is a worse experience than a form alone on a phone.
 * So this is hidden rather than reflowed: below `lg` the screen is the form,
 * centred, with the ambient wash `AuthLayout` puts behind it standing in for
 * the panel's depth. Nothing here is load-bearing — it is the same argument the
 * layout makes for showing an empty sidebar next to a login form.
 */
export function AuthPanel({ variant, className }: { variant: PanelVariant; className?: string }) {
  const copy = PANEL_COPY[variant]
  const parallax = usePointerParallax()

  return (
    <aside
      onPointerMove={parallax.onPointerMove}
      onPointerLeave={parallax.onPointerLeave}
      className={cn(
        // `hidden lg:flex` lives here rather than at the call site, because a
        // component whose own layout depends on its caller remembering to pass
        // a display utility is a component that breaks the first time it is
        // reused. The reason it is hidden is in the docstring above, so the
        // decision belongs beside the code that makes it.
        'relative hidden flex-col overflow-hidden rounded-4xl bg-surface-inverse text-ink-inverse lg:flex',
        className,
      )}
    >
      <AuthPanelGlow />

      <div className="relative flex flex-1 flex-col p-10 xl:p-14">
        <Enter delay={120} y={10}>
          <span className="eyebrow inline-flex items-center gap-2 rounded-full border border-ink-inverse/12 px-3 py-1 text-ink-inverse/60">
            <span aria-hidden="true" className="size-1.5 rounded-full bg-brand" />
            {copy.eyebrow}
          </span>
        </Enter>

        {/* The cards sit in the optical centre, between the eyebrow and the
            headline, and take whatever height is left over. */}
        <div className="flex flex-1 items-center py-12">
          <AuthPanelCards cards={copy.cards} x={parallax.x} y={parallax.y} overlap={variant === 'login' || variant === 'register'} />
        </div>

        <div className="flex flex-col gap-4">
          <Enter delay={520} y={16}>
            <h2 className="text-2xl font-semibold tracking-[-0.025em] text-balance xl:text-[2rem] xl:leading-[1.15]">
              {copy.headline}
            </h2>
          </Enter>

          <Enter delay={600} y={16}>
            <p className="text-[0.9375rem] leading-relaxed text-ink-inverse/70">{copy.body}</p>
          </Enter>

          <Enter delay={680} y={16}>
            <p className="border-t border-ink-inverse/10 pt-4 text-[0.8125rem] leading-relaxed text-ink-inverse/55">
              {copy.footnote}
            </p>
          </Enter>
        </div>
      </div>
    </aside>
  )
}
