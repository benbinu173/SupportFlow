import { type FormEvent, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'

import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Field } from '@/components/ui/Field'
import { Input } from '@/components/ui/Input'
import { AuthAlert } from '@/features/auth/AuthAlert'
import { useAuthenticate } from '@/features/auth/useAuthenticate'
import { login } from '@/services/api/auth'
import { fieldError } from '@/services/api/errors'

/** What `RequireAuth` stashes before it redirects here. */
interface RedirectState {
  from?: string
}

export function LoginPage() {
  const location = useLocation()
  const { authenticate, pending, error } = useAuthenticate()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const returnTo = (location.state as RedirectState | null)?.from
  const fieldErrors = error?.fieldErrors ?? []

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    // Not `void`: leaving it unawaited would mean the form could be submitted
    // again while the first attempt is still in flight, and `pending` is what
    // disables the button. The handler returning a promise is fine — React
    // ignores a form handler's return value.
    await authenticate(() => login({ email, password }), returnTo)
  }

  return (
    <Card variant="bezel">
      <div className="flex flex-col gap-5 p-5">
        <header className="flex flex-col gap-1">
          <h1 className="text-lg font-semibold tracking-[-0.01em] text-ink">Sign in</h1>
          <p className="text-[0.8125rem] text-ink-muted">
            Welcome back. Pick up where your queue left off.
          </p>
        </header>

        <AuthAlert error={error} />

        <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
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
              }}
            />
          </Field>

          {/* No `minLength` here, deliberately.
              The server validates login passwords as a plain string with no
              policy (`schemas/auth.py` explains why: a length check would reject
              a too-short password with a 422 *before* any credential check,
              which distinguishes "wrong length" from "wrong password" and so
              leaks the policy). A client-side minimum would reintroduce exactly
              that, and would also lock out anyone whose password predates a
              policy change. */}
          <Field label="Password" required error={fieldError(fieldErrors, 'password')}>
            <Input
              type="password"
              name="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => {
                setPassword(event.target.value)
              }}
            />
          </Field>

          <Button type="submit" size="lg" fullWidth loading={pending} className="mt-1">
            Sign in
          </Button>
        </form>

        <p className="text-center text-[0.8125rem] text-ink-muted">
          No account yet?{' '}
          <Link
            to="/register"
            className="rounded font-medium text-brand-text underline-offset-4 transition-colors duration-150 ease-out-expo hover:underline"
          >
            Create one
          </Link>
        </p>
      </div>
    </Card>
  )
}
