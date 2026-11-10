import { CheckCircle } from '@phosphor-icons/react'
import { useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { Enter } from '@/components/motion/Enter'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Field } from '@/components/ui/Field'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { AuthAlert } from '@/features/auth/AuthAlert'
import { PasswordChecklist } from '@/features/auth/PasswordChecklist'
import { PASSWORD_MAX_LENGTH, passwordLengthProblem } from '@/features/auth/password-policy'
import { resetPassword } from '@/services/api/auth'
import { ApiError, ERROR_CODES, fieldError, toApiError } from '@/services/api/errors'

export function ResetPasswordPage() {
  const queryClient = useQueryClient()
  const location = useLocation()
  const navigate = useNavigate()
  const [token, setToken] = useState(() => new URLSearchParams(location.hash.slice(1)).get('token') ?? '')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [pending, setPending] = useState(false)
  const [completed, setCompleted] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  useEffect(() => {
    // Keep the credential only in component memory after reading the email fragment.
    if (location.hash) navigate(location.pathname + location.search, { replace: true })
  }, [location.hash, location.pathname, location.search, navigate])

  const invalidLink = !token || error?.code === ERROR_CODES.INVALID_RESET_TOKEN

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending || invalidLink) return
    const policyError = passwordLengthProblem(password)
    if (policyError) {
      setError(policyError)
      return
    }
    if (password !== confirmation) {
      setError(new ApiError({ code: ERROR_CODES.VALIDATION_ERROR, status: null, message: "Passwords don't match.", fieldErrors: [{ field: 'confirmation', message: "Passwords don't match." }] }))
      return
    }
    setPending(true)
    setError(null)
    try {
      await resetPassword({ token, password })
      queryClient.clear()
      setToken('')
      setPassword('')
      setConfirmation('')
      setCompleted(true)
    } catch (cause) {
      setError(toApiError(cause))
    } finally {
      setPending(false)
    }
  }

  return (
    <Enter y={18}>
      <Card variant="bezel">
        <div className="flex flex-col gap-5 p-5 sm:p-6">
          <header className="flex flex-col gap-1.5">
            <h1 className="text-lg font-semibold tracking-[-0.01em] text-ink">
              {completed ? 'Password updated' : invalidLink ? 'Request a new reset link' : 'Set a new password'}
            </h1>
            <p role={invalidLink && !completed ? 'alert' : undefined} className="text-[0.8125rem] leading-relaxed text-ink-muted">
              {completed ? 'Your previous sessions have ended. Sign in with your new password.' : invalidLink ? 'This reset link is invalid or has expired. Request a new one to continue.' : 'Choose a password you use only for this workspace.'}
            </p>
          </header>
          {completed ? (
            <>
              <div role="status" className="flex items-center gap-2 rounded-lg border border-success-border bg-success-subtle px-3 py-3 text-[0.8125rem] text-success-text">
                <CheckCircle aria-hidden="true" weight="light" className="size-5 shrink-0" />Your new password is ready.
              </div>
              <Link to="/login" className="rounded-full bg-brand px-4 py-3 text-center text-sm font-medium text-brand-fg transition-opacity hover:opacity-90">Sign in</Link>
            </>
          ) : invalidLink ? (
            <Link to="/forgot-password" className="rounded-full bg-brand px-4 py-3 text-center text-sm font-medium text-brand-fg transition-opacity hover:opacity-90">Request reset link</Link>
          ) : (
            <>
              <AuthAlert error={error} />
              <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
                <Field label="New password" required error={fieldError(error?.fieldErrors ?? [], 'password')}>
                  <PasswordInput name="password" autoComplete="new-password" autoFocus required maxLength={PASSWORD_MAX_LENGTH} value={password} onChange={(event) => setPassword(event.target.value)} />
                </Field>
                <PasswordChecklist password={password} />
                <Field label="Confirm password" required error={fieldError(error?.fieldErrors ?? [], 'confirmation')}>
                  <PasswordInput name="confirmation" autoComplete="new-password" required maxLength={PASSWORD_MAX_LENGTH} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
                </Field>
                <Button type="submit" size="lg" fullWidth loading={pending} className="rounded-full">Update password</Button>
              </form>
              <Link to="/forgot-password" className="rounded text-center text-[0.8125rem] font-medium text-brand-text underline-offset-4 hover:underline">Request a new link</Link>
            </>
          )}
        </div>
      </Card>
    </Enter>
  )
}
