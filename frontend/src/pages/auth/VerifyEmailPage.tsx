import { ArrowLeft, CheckCircle, EnvelopeSimple } from '@phosphor-icons/react'
import { useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { Enter } from '@/components/motion/Enter'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Field } from '@/components/ui/Field'
import { Input } from '@/components/ui/Input'
import { AuthAlert } from '@/features/auth/AuthAlert'
import { logout, resendVerification, verifyEmail } from '@/services/api/auth'
import { type ApiError, ERROR_CODES, fieldError, toApiError } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'

interface VerificationState {
  email?: string
  requested?: boolean
  from?: string
}

export function VerifyEmailPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const state = location.state as VerificationState | null
  const [token, setToken] = useState(() => new URLSearchParams(location.hash.slice(1)).get('token') ?? '')
  const [email, setEmail] = useState(state?.email ?? '')
  const [requested, setRequested] = useState(state?.requested ?? false)
  const [completed, setCompleted] = useState(false)
  const [organizationSlug, setOrganizationSlug] = useState<string>()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const action = useRef(0)

  useEffect(() => {
    // A mail link can arrive on this same route while the recipient is waiting.
    // Capture each incoming fragment, and discard results belonging to an earlier link.
    if (location.hash) {
      action.current += 1
      // oxlint-disable-next-line react/set-state-in-effect -- Capture external mail-link navigation before erasing its fragment.
      setToken(new URLSearchParams(location.hash.slice(1)).get('token') ?? '')
      setCompleted(false)
      setError(null)
      setPending(false)
      navigate(location.pathname + location.search, { replace: true, state: location.state })
    }
  }, [location.hash, location.pathname, location.search, location.state, navigate])

  const invalidLink = error?.code === ERROR_CODES.INVALID_VERIFICATION_TOKEN
  const canConfirm = Boolean(token) && !invalidLink

  async function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending || !canConfirm) return
    const current = ++action.current
    setPending(true)
    setError(null)
    try {
      const result = await verifyEmail(token)
      if (current !== action.current) return
      setToken('')
      setEmail(result.email)
      setOrganizationSlug(result.organization_slug)
      setCompleted(true)
    } catch (cause) {
      if (current !== action.current) return
      const failure = toApiError(cause)
      setError(failure)
      if (failure.code === ERROR_CODES.INVALID_VERIFICATION_TOKEN) setToken('')
    } finally {
      if (current === action.current) setPending(false)
    }
  }

  async function resend(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending) return
    const current = ++action.current
    setPending(true)
    setError(null)
    try {
      await resendVerification(email)
      if (current !== action.current) return
      setToken('')
      setRequested(true)
    } catch (cause) {
      if (current !== action.current) return
      setError(toApiError(cause))
    } finally {
      if (current === action.current) setPending(false)
    }
  }

  async function continueToSignIn() {
    if (pending) return
    setPending(true)
    try {
      // Switching is an explicit click. Opening a link preserves another account's session.
      if (useSessionStore.getState().status === 'authenticated') await logout()
    } catch {
      useSessionStore.getState().endSession()
    } finally {
      queryClient.clear()
      navigate('/login', { replace: true, state: { email, organizationSlug, from: state?.from } })
    }
  }

  let heading = 'Verify your email'
  let description = "If an active account still needs verification, we'll send a link to its email address."
  if (completed) {
    heading = 'Email verified'
    description = 'Your email is confirmed. Sign in with your password to open its workspace.'
  } else if (canConfirm) {
    description = 'Confirm that this email address belongs to you before signing in.'
  } else if (invalidLink) {
    heading = 'Request a new verification link'
    description = 'This link is invalid, has expired, or has already been used. Request another to continue.'
  } else if (requested) {
    heading = 'Check your email'
  }

  return (
    <Enter y={18}>
      <Card variant="bezel">
        <div className="flex flex-col gap-5 p-5 sm:p-6">
          <header className="flex flex-col gap-1.5">
            <h1 className="text-lg font-semibold tracking-[-0.01em] text-ink">{heading}</h1>
            <p className="text-[0.8125rem] leading-relaxed text-ink-muted" role={invalidLink ? 'alert' : undefined}>{description}</p>
          </header>
          {completed ? (
            <>
              <div role="status" className="flex items-start gap-2 rounded-lg border border-success-border bg-success-subtle px-3 py-3 text-[0.8125rem] text-success-text">
                <CheckCircle aria-hidden="true" weight="light" className="mt-0.5 size-5 shrink-0" />
                <p className="break-all leading-relaxed">{email} is ready to use.</p>
              </div>
              <Button size="lg" fullWidth loading={pending} className="rounded-full" onClick={() => { void continueToSignIn() }}>Continue to sign in</Button>
            </>
          ) : canConfirm ? (
            <>
              <AuthAlert error={error} />
              <form onSubmit={confirm} className="flex flex-col gap-4">
                <Button type="submit" size="lg" fullWidth loading={pending} className="rounded-full">Verify email</Button>
              </form>
            </>
          ) : (
            <>
              {requested && (
                <div role="status" className="flex items-start gap-3 rounded-lg border border-border-subtle bg-surface px-3 py-3 text-[0.8125rem] text-ink-muted">
                  <EnvelopeSimple aria-hidden="true" weight="light" className="mt-0.5 size-5 shrink-0 text-brand-text" />
                  <p className="leading-relaxed">Look for a SupportFlow email at <span className="break-all font-medium text-ink">{email}</span>. Each link names its workspace. Check your spam folder, too.</p>
                </div>
              )}
              {!invalidLink && <AuthAlert error={error} />}
              <form onSubmit={resend} noValidate className="flex flex-col gap-4">
                <Field label="Email" required error={fieldError(error?.fieldErrors ?? [], 'email')}>
                  <Input type="email" name="email" autoComplete="email" autoFocus={!requested} required value={email} onChange={(event) => setEmail(event.target.value)} />
                </Field>
                <Button type="submit" size="lg" fullWidth loading={pending} className="rounded-full">{requested ? 'Resend verification email' : 'Send verification link'}</Button>
              </form>
            </>
          )}
          {!completed && <Link to="/login" className="inline-flex items-center justify-center gap-1.5 rounded text-[0.8125rem] font-medium text-brand-text underline-offset-4 hover:underline"><ArrowLeft aria-hidden="true" weight="light" className="size-3.5" />Back to sign in</Link>}
        </div>
      </Card>
    </Enter>
  )
}
