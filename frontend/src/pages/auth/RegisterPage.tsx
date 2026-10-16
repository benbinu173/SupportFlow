import { type FormEvent, useState } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Field } from '@/components/ui/Field'
import { Input } from '@/components/ui/Input'
import { AuthAlert } from '@/features/auth/AuthAlert'
import { useAuthenticate } from '@/features/auth/useAuthenticate'
import { register } from '@/services/api/auth'
import { ApiError, ERROR_CODES, fieldError } from '@/services/api/errors'

/**
 * The password window the server enforces, mirrored here.
 *
 * `app/schemas/fields.py` reads these from settings, so they are the defaults
 * rather than an absolute truth — but the alternative is a round trip that
 * reports a rule this form already knew about. Being wrong in this direction
 * costs nothing; being wrong in the other direction costs a rejected password
 * after the fact.
 *
 * Length only, with no composition rules, matching the policy: NIST SP 800-63B
 * is that character-class requirements push people toward `P@ssw0rd1` without
 * adding entropy, and a length floor does add it.
 *
 * These constants are only as good as `passwordLengthProblem` below — see its
 * note on why the browser will not enforce them for us.
 */
const PASSWORD_MIN_LENGTH = 12
const PASSWORD_MAX_LENGTH = 128

/**
 * The one check this form makes before spending a request.
 *
 * **The form is `noValidate`, which switches off the browser's own constraint
 * validation.** `minLength`, `maxLength` and `required` on the inputs are
 * therefore inert as far as submission is concerned — they remain in the markup
 * for password managers and assistive technology, but nothing acts on them. That
 * setting is deliberate: the design system renders its own field errors, and a
 * native validation bubble in the middle of it is a second, worse voice saying
 * the same thing. The consequence is that these bounds have to be checked here
 * or not at all, and "not at all" means the request goes out and comes back a
 * 422 — exactly the round trip this page claims to avoid.
 *
 * The two bounds are not symmetrical, which is the whole reason this function is
 * shaped the way it is. `maxLength` is enforced *while typing* — the browser
 * truncates the field at 128 characters before value ever reaches React, and
 * `noValidate` has nothing to do with that. `minLength` is enforced only at
 * submission, which is the machinery `noValidate` disables, so a five-character
 * password sails through the input, through the form, and onto the wire. The
 * floor is therefore the bound that needed code; the ceiling is here because the
 * length of a value this form will happily *hold* should not depend on a
 * browser behaviour that a password manager's autofill can bypass.
 *
 * Deliberately length only, and deliberately silent on an empty field.
 *
 * Length only, because everything else the server checks arrives as a 422 whose
 * `details` already render on the field that caused it, which is actionable. An
 * email that is not an address is not worth a second implementation here that
 * could disagree with Pydantic's.
 *
 * Silent when the field is empty, because in that case the user has left more
 * than the password blank and the server will name every one of them in a single
 * response. Refusing locally would show one problem, and the other three only
 * after another submit.
 */
function passwordLengthProblem(password: string): ApiError | null {
  if (password.length === 0) {
    return null
  }

  const problem =
    password.length < PASSWORD_MIN_LENGTH
      ? `Use at least ${String(PASSWORD_MIN_LENGTH)} characters.`
      : password.length > PASSWORD_MAX_LENGTH
        ? `Use no more than ${String(PASSWORD_MAX_LENGTH)} characters.`
        : null

  if (problem === null) {
    return null
  }

  // Shaped exactly like the server's validation failure, so it travels the same
  // path to the same place: `fieldError` finds it by field name, and `AuthAlert`
  // suppresses its own banner because there are field errors to show instead.
  return new ApiError({
    message: 'The request could not be processed.',
    code: ERROR_CODES.VALIDATION_ERROR,
    status: null,
    fieldErrors: [{ field: 'password', message: problem }],
  })
}

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
    <Card variant="bezel">
      <div className="flex flex-col gap-5 p-5">
        <header className="flex flex-col gap-1">
          <h1 className="text-lg font-semibold tracking-[-0.01em] text-ink">Create your workspace</h1>
          <p className="text-[0.8125rem] text-ink-muted">
            Sets up a new organization with you as its administrator.
          </p>
        </header>

        <AuthAlert error={shownError} />

        <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
          {/* Asked for first because it changes what the rest means: this form
              creates a tenant, and the account it produces is necessarily an
              admin of it (`schemas/auth.py` — there is no way to join an
              existing organization through this endpoint). A user who expected
              to be joining their team's workspace should find that out here,
              not after they have an empty one. */}
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

          <Field
            label="Password"
            hint={`At least ${String(PASSWORD_MIN_LENGTH)} characters.`}
            required
            error={fieldError(fieldErrors, 'password')}
          >
            <Input
              type="password"
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
          </Field>

          <Button type="submit" size="lg" fullWidth loading={pending} className="mt-1">
            Create workspace
          </Button>
        </form>

        <p className="text-center text-[0.8125rem] text-ink-muted">
          Already have an account?{' '}
          <Link
            to="/login"
            className="rounded font-medium text-brand-text underline-offset-4 transition-colors duration-150 ease-out-expo hover:underline"
          >
            Sign in
          </Link>
        </p>
      </div>
    </Card>
  )
}
