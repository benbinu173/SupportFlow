import { ArrowLeft, EnvelopeSimple } from '@phosphor-icons/react'
import { type FormEvent, useState } from 'react'
import { Link } from 'react-router-dom'

import { Enter } from '@/components/motion/Enter'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Field } from '@/components/ui/Field'
import { Input } from '@/components/ui/Input'
import { AuthAlert } from '@/features/auth/AuthAlert'
import { requestPasswordReset } from '@/services/api/auth'
import { type ApiError, fieldError, toApiError } from '@/services/api/errors'

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [submitted, setSubmitted] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending) return
    setPending(true)
    setError(null)
    try {
      await requestPasswordReset(email)
      setSubmitted(true)
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
              {submitted ? 'Check your email' : 'Forgot your password?'}
            </h1>
            <p className="text-[0.8125rem] leading-relaxed text-ink-muted">
              {submitted
                ? "If an active account matches your email, we'll send a link to reset its password."
                : "Enter your work email and we'll help you get back to your workspace."}
            </p>
          </header>
          {submitted ? (
            <div role="status" className="flex flex-col gap-4">
              <div className="flex items-start gap-3 rounded-lg border border-border-subtle bg-surface px-3 py-3 text-[0.8125rem] text-ink-muted">
                <EnvelopeSimple aria-hidden="true" weight="light" className="mt-0.5 size-5 shrink-0 text-brand-text" />
                <p className="leading-relaxed">
                  Look for a SupportFlow email at <span className="break-all font-medium text-ink">{email}</span>.
                  {' '}Each reset link names its workspace. Check your spam folder, too.
                </p>
              </div>
              <Button variant="secondary" onClick={() => setSubmitted(false)}>Try another email</Button>
            </div>
          ) : (
            <>
              <AuthAlert error={error} />
              <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
                <Field label="Email" required error={fieldError(error?.fieldErrors ?? [], 'email')}>
                  <Input type="email" name="email" autoComplete="email" autoFocus required value={email} onChange={(event) => setEmail(event.target.value)} />
                </Field>
                <Button type="submit" size="lg" fullWidth loading={pending} className="rounded-full">Send reset link</Button>
              </form>
            </>
          )}
          <Link to="/login" className="inline-flex items-center justify-center gap-1.5 rounded text-[0.8125rem] font-medium text-brand-text underline-offset-4 hover:underline">
            <ArrowLeft aria-hidden="true" weight="light" className="size-3.5" />Back to sign in
          </Link>
        </div>
      </Card>
    </Enter>
  )
}
