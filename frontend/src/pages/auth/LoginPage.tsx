import { ArrowRight } from '@phosphor-icons/react'
import { type FormEvent, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { Enter } from '@/components/motion/Enter'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Field } from '@/components/ui/Field'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { AuthAlert } from '@/features/auth/AuthAlert'
import { useAuthenticate } from '@/features/auth/useAuthenticate'
import { login } from '@/services/api/auth'
import { ERROR_CODES, fieldError } from '@/services/api/errors'

/** What `RequireAuth` stashes before it redirects here. */
interface RedirectState {
  from?: string
  email?: string
  organizationSlug?: string
}

/**
 * The sign-in form.
 *
 * The copy here is deliberately flat — "Sign in", "Use your work email and
 * password." — because the panel beside it is doing the talking. Two voices
 * making the same point on one screen read as one voice stuttering, so the page
 * states what to type and the panel states what it is for.
 *
 * The entry is a stagger rather than a single fade: the card, then its header,
 * then each field in turn, then the button. It is the one place in the product
 * where a longer entrance is correct, because the page has nothing else to do —
 * an agent opening their queue must not watch twenty rows do this, which is why
 * ADR-010 calibrates marketing and auth screens apart from dense ones.
 */
export function LoginPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const { authenticate, pending, error } = useAuthenticate()

  const [email, setEmail] = useState((location.state as RedirectState | null)?.email ?? '')
  const [password, setPassword] = useState('')
  const [organizationSlug, setOrganizationSlug] = useState((location.state as RedirectState | null)?.organizationSlug)

  const returnTo = (location.state as RedirectState | null)?.from
  const fieldErrors = error?.fieldErrors ?? []

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    // Not `void`: leaving it unawaited would mean the form could be submitted
    // again while the first attempt is still in flight, and `pending` is what
    // disables the button. The handler returning a promise is fine — React
    // ignores a form handler's return value.
    const payload = { email, password, ...(organizationSlug ? { organization_slug: organizationSlug } : {}) }
    const failure = await authenticate(() => login(payload), returnTo)
    if (failure?.code === ERROR_CODES.EMAIL_NOT_VERIFIED) {
      navigate('/verify-email', { state: { email, from: returnTo } })
    }
  }

  return (
    <Enter y={18}>
      <Card variant="bezel">
        <div className="flex flex-col gap-5 p-5 sm:p-6">
          <Enter delay={160} y={10}>
            <header className="flex flex-col gap-1.5">
              {/* The heading text is load-bearing: `router.test.tsx` navigates
                  to `/login` and asserts this screen answers, by it. */}
              <h1 className="text-lg font-semibold tracking-[-0.01em] text-ink">Sign in</h1>
              <p className="text-[0.8125rem] text-ink-muted">Use your work email and password.</p>
            </header>
          </Enter>

          <AuthAlert error={error} />

          <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
            <Enter delay={240} y={10}>
              <Field label="Email" required error={fieldError(fieldErrors, 'email')}>
                <Input
                  type="email"
                  name="email"
                  autoComplete="email"
                  autoFocus
                  required
                  value={email}
                  onChange={(event) => {
                    setEmail(event.target.value)
                    setOrganizationSlug(undefined)
                  }}
                />
              </Field>
            </Enter>

            {/* No `minLength` here, deliberately.
                The server validates login passwords as a plain string with no
                policy (`schemas/auth.py` explains why: a length check would reject
                a too-short password with a 422 *before* any credential check,
                which distinguishes "wrong length" from "wrong password" and so
                leaks the policy). A client-side minimum would reintroduce exactly
                that, and would also lock out anyone whose password predates a
                policy change. */}
            <Enter delay={300} y={10}>
              <Field label="Password" required error={fieldError(fieldErrors, 'password')}>
                <PasswordInput
                  name="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(event) => {
                    setPassword(event.target.value)
                  }}
                />
              </Field>
            </Enter>

            <Enter delay={380} y={10}>
              <div className="mb-4 text-right">
                <Link
                  to="/forgot-password"
                  className="rounded text-[0.8125rem] font-medium text-brand-text underline-offset-4 hover:underline"
                >
                  Forgot password?
                </Link>
              </div>
              {/* The marketing surface's pill CTA, with the arrow nested in its
                  own disc rather than sitting beside the label. `buttonClasses`
                  merges a caller's `className` last, which is what lets
                  `rounded-full` win over the size's radius — the shape ADR-010
                  reserves for a call to action. */}
              <Button
                type="submit"
                size="lg"
                fullWidth
                loading={pending}
                className="group mt-1 rounded-full"
                trailingIcon={
                  <span
                    aria-hidden="true"
                    className="grid size-6 place-items-center rounded-full bg-brand-fg/15 transition-transform duration-300 ease-out-expo group-hover:translate-x-0.5"
                  >
                    <ArrowRight weight="bold" className="size-3.5" />
                  </span>
                }
              >
                Sign in
              </Button>
            </Enter>
          </form>

          <Enter delay={460} y={10}>
            <p className="mb-3 text-center text-[0.8125rem] text-ink-muted">
              Need to verify your email?{' '}
              <Link to="/verify-email" className="rounded font-medium text-brand-text underline-offset-4 hover:underline">Resend link</Link>
            </p>
            <p className="text-center text-[0.8125rem] text-ink-muted">
              No account yet?{' '}
              <Link
                to="/register"
                className="rounded font-medium text-brand-text underline-offset-4 transition-colors duration-150 ease-out-expo hover:underline"
              >
                Create one
              </Link>
            </p>
          </Enter>
        </div>
      </Card>
    </Enter>
  )
}
