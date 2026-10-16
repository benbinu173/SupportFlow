import { Timer, WarningCircle } from '@phosphor-icons/react'

import { ERROR_CODES, type ApiError } from '@/services/api/errors'

/**
 * The form-level failure notice.
 *
 * Renders nothing when the error carries field errors, because those belong
 * beside the inputs they name — a banner repeating "that email is already
 * registered" while the email field also says so is the same news twice, and
 * the version away from the field is the one the user has to hunt for.
 *
 * `RATE_LIMITED` gets its own wording rather than the generic message, which is
 * the distinction `backend/app/core/exceptions.py` goes to the trouble of
 * encoding: "too many attempts, wait 42 seconds" tells the user what to do,
 * and "something went wrong" invites them to keep pressing the button, which is
 * the one action guaranteed not to work. `Retry-After` is parsed into
 * `retryAfterSeconds` for exactly this.
 */
export function AuthAlert({ error }: { error: ApiError | null }) {
  if (!error || error.fieldErrors.length > 0) {
    return null
  }

  const rateLimited = error.code === ERROR_CODES.RATE_LIMITED
  const wait = error.retryAfterSeconds

  return (
    <div
      role="alert"
      className="flex items-start gap-2.5 rounded-lg border border-danger-border bg-danger-subtle px-3 py-2.5 text-[0.8125rem] text-danger-text"
    >
      <span aria-hidden="true" className="mt-px flex shrink-0 [&_svg]:size-4">
        {rateLimited ? <Timer weight="light" /> : <WarningCircle weight="light" />}
      </span>

      <p className="leading-snug">
        {rateLimited ? (
          wait === null ? (
            'Too many attempts. Wait a moment, then try again.'
          ) : (
            `Too many attempts. Try again in ${wait} second${wait === 1 ? '' : 's'}.`
          )
        ) : (
          error.message
        )}
      </p>
    </div>
  )
}
