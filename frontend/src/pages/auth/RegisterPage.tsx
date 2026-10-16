import { ArrowRight } from '@phosphor-icons/react'
import { type FormEvent, useState } from 'react'
import { Link } from 'react-router-dom'

import { Enter } from '@/components/motion/Enter'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Field } from '@/components/ui/Field'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { AuthAlert } from '@/features/auth/AuthAlert'
import { PasswordChecklist } from '@/features/auth/PasswordChecklist'
import {
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  passwordLengthProblem,
} from '@/features/auth/password-policy'
import { useAuthenticate } from '@/features/auth/useAuthenticate'
import { register } from '@/services/api/auth'
import { ApiError, fieldError } from '@/services/api/errors'

/**
 * The registration form.
 *
 * Registration reads differently from sign-in on purpose, and the difference is
 * not decoration: this endpoint creates a **tenant**, not a member of one, so
 * the organization is asked for first and the rest of the form is explained by
 * the answer. `schemas/auth.py` has no path where an account joins a workspace
 * that already exists, and a user who expected to be joining their team's
 * should find that out from the header rather than after they own an empty one.
 *
 * The password field carries a live checklist rather than a hint, because the
 * rules are knowable before they are broken and because this is the one field
 * whose rules the server enforces rather than merely checking. The checklist
 * and the submit-time refusal read the same two numbers from
 * `features/auth/password-policy`, so the tick marks cannot disagree with the
 * message that follows them.
 */
export function RegisterPage() {
  const { authenticate, pending, error } = useAuthenticate()

  const [organizationName, setOrganizationName] = useState('')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [localError, setLocalError] = useState<ApiError | null>(null)

  // A refusal this form made wins over one the server made, because they are
  // not contemporaries: `localError` is built from the fields as they are right
  // now, while `error` describes a request that has since been superseded. The
  // server's message is not wrong, it is just older than the password the user
  // is looking at.
  const shownError = localError ?? error
  const fieldErrors = shownError?.fieldErrors ?? []

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const problem = passwordLengthProblem(password)

    if (problem !== null) {
      setLocalError(problem)
      return
    }

    setLocalError(null)

    await authenticate(() =>
      register({
        organization_name: organizationName,
        name,
        email,
        password,
      }),
    )
  }

  return (
    <Enter y={18}>
      <Card variant="bezel">
        <div className="flex flex-col gap-5 p-5 sm:p-6">
          <Enter delay={160} y={10}>
            <header className="flex flex-col gap-1.5">
              {/* Load-bearing copy: `router.test.tsx` navigates to `/register`
                  and identifies this screen by this heading. */}
              <h1 className="text-lg font-semibold tracking-[-0.01em] text-ink">
                Create your workspace
              </h1>
              <p className="text-[0.8125rem] text-ink-muted">Set up your team's workspace.</p>
            </header>
          </Enter>

          <AuthAlert error={shownError} />

          <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
            {/* Asked first because it changes what the rest of the form means:
                this endpoint creates a tenant, and the account it produces is
                necessarily an admin of it. A user who expected to be joining
                their team's workspace should find that out here. */}
            <Enter delay={230} y={10}>
              <Field
                label="Organization"
                hint="Your company's name. You can change it later."
                required
                error={fieldError(fieldErrors, 'organization_name')}
              >
                <Input
                  type="text"
                  name="organization_name"
                  autoComplete="organization"
                  autoFocus
                  required
                  maxLength={200}
                  value={organizationName}
                  onChange={(event) => {
                    setOrganizationName(event.target.value)
                  }}
                />
              </Field>
            </Enter>

            <Enter delay={280} y={10}>
              <Field label="Your name" required error={fieldError(fieldErrors, 'name')}>
                <Input
                  type="text"
                  name="name"
                  autoComplete="name"
                  required
                  maxLength={200}
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value)
                  }}
                />
              </Field>
            </Enter>

            <Enter delay={330} y={10}>
              <Field
                label="Work email"
                hint="This becomes your sign-in address."
                required
                error={fieldError(fieldErrors, 'email')}
              >
                <Input
                  type="email"
                  name="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(event) => {
                    setEmail(event.target.value)
                  }}
                />
              </Field>
            </Enter>

            {/* No `hint` here — the checklist below the input is the hint, and
                it is the same copy with a live answer attached. `minLength` and
                `maxLength` stay in the markup for password managers and
                assistive technology even though the form's `noValidate` means
                neither is enforced at submission; see `password-policy.ts`. */}
            <Enter delay={380} y={10}>
              <Field label="Password" required error={fieldError(fieldErrors, 'password')}>
                <PasswordInput
                  name="password"
                  autoComplete="new-password"
                  required
                  minLength={PASSWORD_MIN_LENGTH}
                  maxLength={PASSWORD_MAX_LENGTH}
                  value={password}
                  onChange={(event) => {
                    setPassword(event.target.value)
                  }}
                />
                <PasswordChecklist password={password} className="mt-0.5" />
              </Field>
            </Enter>

            <Enter delay={450} y={10}>
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
                Create workspace
              </Button>
            </Enter>
          </form>

          <Enter delay={520} y={10}>
            <p className="text-center text-[0.8125rem] text-ink-muted">
              Already have an account?{' '}
              <Link
                to="/login"
                className="rounded font-medium text-brand-text underline-offset-4 transition-colors duration-150 ease-out-expo hover:underline"
              >
                Sign in
              </Link>
            </p>
          </Enter>
        </div>
      </Card>
    </Enter>
  )
}
