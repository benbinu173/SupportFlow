import { AxiosError } from 'axios'

/**
 * Reading the API's §42 error envelope.
 *
 * The server guarantees one shape for every failure it emits deliberately:
 *
 *     {"error": {"code": "INVALID_CREDENTIALS", "message": "Incorrect email or password."}}
 *
 * and one addition for validation failures, where Pydantic's per-field problems
 * are forwarded as `details`:
 *
 *     {"error": {...}, "details": [{"field": "email", "message": "..."}]}
 *
 * `app/main.py` renders the framework's own errors — unknown path, wrong method
 * — in the same envelope rather than Starlette's default `{"detail": ...}`,
 * precisely so a client needs one parser. This is that parser.
 *
 * Nothing here invents an error the server did not send. A failure that did not
 * come from the API — a dropped connection, a DNS failure — becomes
 * `NETWORK_ERROR`, which is a claim about *this* client's observation and not a
 * claim about the server's answer.
 */

/**
 * The codes this client branches on by name.
 *
 * Deliberately not the full `ErrorCode` enum from
 * `backend/app/core/exceptions.py`. A code belongs here only once something in
 * this app treats it differently from every other code — a list that mirrors the
 * server wholesale drifts into a lookup table nobody reads, and the two dozen
 * `*_NOT_FOUND` variants are all the same experience.
 */
export const ERROR_CODES = {
  /** No token, or one that failed verification. The refresh flow keys on this. */
  AUTHENTICATION_REQUIRED: 'AUTHENTICATION_REQUIRED',
  /** Bad email or password. Distinct from the above so login can say so. */
  INVALID_CREDENTIALS: 'INVALID_CREDENTIALS',
  INVALID_RESET_TOKEN: 'INVALID_RESET_TOKEN',
  INVALID_VERIFICATION_TOKEN: 'INVALID_VERIFICATION_TOKEN',
  EMAIL_NOT_VERIFIED: 'EMAIL_NOT_VERIFIED',
  /** Valid identity, insufficient rights — or a deactivated account. */
  FORBIDDEN: 'FORBIDDEN',
  VALIDATION_ERROR: 'VALIDATION_ERROR',
  RATE_LIMITED: 'RATE_LIMITED',
  /** §5's illegal status change. Rendered as a conflict, not a generic failure. */
  INVALID_TICKET_TRANSITION: 'INVALID_TICKET_TRANSITION',
  USER_ALREADY_EXISTS: 'USER_ALREADY_EXISTS',
  CUSTOMER_ALREADY_EXISTS: 'CUSTOMER_ALREADY_EXISTS',
  INTERNAL_SERVER_ERROR: 'INTERNAL_SERVER_ERROR',
  /** Synthetic: the request never reached the server. */
  NETWORK_ERROR: 'NETWORK_ERROR',
  /** Synthetic: the request failed in a way this client cannot classify. */
  UNKNOWN: 'UNKNOWN',
} as const

export type ErrorCode = (typeof ERROR_CODES)[keyof typeof ERROR_CODES]

export interface FieldError {
  /** The server's path, already flattened — `"email"`, or `"items.0.name"`. */
  field: string
  message: string
}

interface ErrorEnvelope {
  error?: { code?: unknown; message?: unknown }
  details?: unknown
}

export class ApiError extends Error {
  /** HTTP status, or `null` when the request never got one. */
  readonly status: number | null
  readonly code: string
  readonly fieldErrors: readonly FieldError[]
  /** From `Retry-After`, when the server sent one. Seconds. */
  readonly retryAfterSeconds: number | null

  constructor(init: {
    message: string
    code: string
    status: number | null
    fieldErrors?: readonly FieldError[]
    retryAfterSeconds?: number | null
    cause?: unknown
  }) {
    super(init.message, init.cause === undefined ? undefined : { cause: init.cause })

    this.name = 'ApiError'
    this.code = init.code
    this.status = init.status
    this.fieldErrors = init.fieldErrors ?? []
    this.retryAfterSeconds = init.retryAfterSeconds ?? null
  }

  /** Whether the server rejected the identity rather than the request. */
  get isAuthFailure(): boolean {
    return (
      this.status === 401 ||
      this.code === ERROR_CODES.AUTHENTICATION_REQUIRED ||
      this.code === ERROR_CODES.INVALID_CREDENTIALS
    )
  }
}

/** A field's message, for wiring into a form's per-input error slot. */
export function fieldError(errors: readonly FieldError[], field: string): string | undefined {
  return errors.find((error) => error.field === field)?.message
}

/**
 * `details` entries, defensively read.
 *
 * The server's handler builds these from `loc` and `msg` and is well-tested, so
 * this is not distrust of a specific bug — it is that a malformed entry should
 * cost one bad message rather than throw while rendering an error, which is the
 * one moment the app is already having a bad time.
 */
function readFieldErrors(value: unknown): FieldError[] {
  if (!Array.isArray(value)) {
    return []
  }

  return value.flatMap((entry) => {
    if (typeof entry !== 'object' || entry === null) {
      return []
    }

    const { field, message } = entry as { field?: unknown; message?: unknown }

    if (typeof field !== 'string' || typeof message !== 'string') {
      return []
    }

    return [{ field, message }]
  })
}

function readRetryAfter(error: AxiosError): number | null {
  const raw = error.response?.headers?.['retry-after']

  if (typeof raw !== 'string') {
    return null
  }

  const seconds = Number.parseInt(raw, 10)

  return Number.isFinite(seconds) ? seconds : null
}

/**
 * Normalise anything thrown by a request into an `ApiError`.
 *
 * The single entry point every caller should use. Feature code should never see
 * an `AxiosError` — that would leak axios's vocabulary (`isAxiosError`,
 * `error.response.data`) into screens, and would mean every screen re-implements
 * the same envelope-reading.
 */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error
  }

  if (!(error instanceof AxiosError)) {
    return new ApiError({
      message: 'Something went wrong.',
      code: ERROR_CODES.UNKNOWN,
      status: null,
      cause: error,
    })
  }

  // No response at all: DNS, connection refused, timeout, CORS preflight
  // refused. Reported as this client's failure to reach the server, because the
  // server never said anything and claiming otherwise would be a fiction.
  if (!error.response) {
    return new ApiError({
      message:
        error.code === 'ECONNABORTED'
          ? 'The server took too long to respond. Try again.'
          : 'Could not reach the server. Check your connection and try again.',
      code: ERROR_CODES.NETWORK_ERROR,
      status: null,
      cause: error,
    })
  }

  const { status, data } = error.response
  const envelope = (data ?? {}) as ErrorEnvelope

  const code = typeof envelope.error?.code === 'string' ? envelope.error.code : ERROR_CODES.UNKNOWN
  const message =
    typeof envelope.error?.message === 'string' && envelope.error.message.length > 0
      ? envelope.error.message
      : 'Something went wrong.'

  return new ApiError({
    message,
    code,
    status,
    fieldErrors: readFieldErrors(envelope.details),
    retryAfterSeconds: readRetryAfter(error),
    cause: error,
  })
}
