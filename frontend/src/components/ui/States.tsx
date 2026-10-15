import { WarningCircle } from '@phosphor-icons/react'
import type { ReactNode } from 'react'

import { cn } from '@/utils/cn'

import { Button } from './Button'

export type EmptyStateProps = {
  icon?: ReactNode
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  className?: string
}

/**
 * What a screen shows when it has nothing to show.
 *
 * The distinction that matters is between "there is nothing here" and "there is
 * nothing here matching what you asked for" — the second needs a way back, and
 * the first does not. Rather than two components, callers pass `action` when
 * there is a way back, which is why the ticket queue's filtered-empty state
 * carries a Clear filters button and its truly-empty state carries a New ticket
 * button.
 */
export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-3 px-6 py-16 text-center',
        className,
      )}
    >
      {icon && (
        <div className="grid size-11 place-items-center rounded-xl border border-border-subtle bg-surface-sunken text-ink-subtle [&_svg]:size-5">
          {icon}
        </div>
      )}

      <div className="flex max-w-sm flex-col gap-1">
        <p className="text-sm font-medium text-ink">{title}</p>
        {description && <p className="text-[0.8125rem] text-ink-muted">{description}</p>}
      </div>

      {action && <div className="mt-1">{action}</div>}
    </div>
  )
}

export type ErrorStateProps = {
  /** Whatever was thrown. Only `message` is read, so an axios error, a plain
   *  Error and a string all work — the alternative is every call site
   *  unwrapping it first and one of them forgetting to. */
  error?: unknown
  title?: ReactNode
  description?: ReactNode
  onRetry?: () => void
  className?: string
}

function messageOf(error: unknown): string | undefined {
  if (typeof error === 'string') {
    return error
  }

  if (error instanceof Error) {
    return error.message
  }

  return undefined
}

/**
 * A failed load, with a way out.
 *
 * The retry button is optional but should almost always be passed: a query that
 * failed because a token was mid-refresh succeeds on the second attempt, and a
 * screen that offers only "something went wrong" makes the user guess that
 * reloading the whole page is the fix.
 */
export function ErrorState({ error, title, description, onRetry, className }: ErrorStateProps) {
  const message = description ?? messageOf(error)

  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col items-center justify-center gap-3 px-6 py-12 text-center',
        className,
      )}
    >
      <div className="grid size-11 place-items-center rounded-xl border border-danger-border bg-danger-subtle text-danger-text [&_svg]:size-5">
        <WarningCircle aria-hidden="true" />
      </div>

      <div className="flex max-w-sm flex-col gap-1">
        <p className="text-sm font-medium text-ink">{title ?? 'Something went wrong'}</p>
        {message && <p className="text-[0.8125rem] text-ink-muted">{message}</p>}
      </div>

      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry} className="mt-1">
          Try again
        </Button>
      )}
    </div>
  )
}
