import type { ComponentPropsWithRef, ReactNode } from 'react'

import { cn } from '@/utils/cn'

export type BadgeVariant =
  | 'neutral'
  | 'brand'
  | 'success'
  | 'warning'
  | 'danger'
  | 'info'
  | 'ai'
  | 'outline'

export type BadgeSize = 'sm' | 'md'

const VARIANT_CLASSES: Record<BadgeVariant, string> = {
  neutral: 'border-border bg-surface-sunken text-ink-muted',
  brand: 'border-brand-border bg-brand-subtle text-brand-text',
  success: 'border-success-border bg-success-subtle text-success-text',
  warning: 'border-warning-border bg-warning-subtle text-warning-text',
  danger: 'border-danger-border bg-danger-subtle text-danger-text',
  info: 'border-info-border bg-info-subtle text-info-text',
  ai: 'border-ai-border bg-ai-subtle text-ai',
  outline: 'border-border bg-transparent text-ink-muted',
}

const SIZE_CLASSES: Record<BadgeSize, string> = {
  sm: 'h-5 gap-1 px-1.5 text-2xs',
  md: 'h-6 gap-1.5 px-2 text-xs',
}

export type BadgeProps = ComponentPropsWithRef<'span'> & {
  variant?: BadgeVariant
  size?: BadgeSize
  /** A leading colour dot. Useful where the badge sits in a dense list and the
   *  colour needs to be findable before the text is read. */
  dot?: boolean
}

/**
 * A small, non-interactive status chip.
 *
 * `rounded-full` rather than the tighter radius the buttons use: a badge is
 * never a target, so it can afford the softer shape that distinguishes it from
 * something clickable at a glance.
 */
export function Badge({
  variant = 'neutral',
  size = 'md',
  dot = false,
  className,
  children,
  ...rest
}: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center rounded-full border font-medium whitespace-nowrap',
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...rest}
    >
      {dot && <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  )
}

/**
 * A count, rendered as a badge that disappears at zero.
 *
 * Unread counts are the case this exists for: a "0" beside Notifications is
 * noise that trains people to stop looking at the badge at all, which is the
 * opposite of what it is for.
 */
export function CountBadge({
  count,
  max = 99,
  className,
  ...rest
}: Omit<BadgeProps, 'children'> & { count: number; max?: number }) {
  if (count <= 0) {
    return null
  }

  return (
    <Badge variant="brand" size="sm" className={cn('tabular-nums', className)} {...rest}>
      {count > max ? `${max}+` : count}
    </Badge>
  )
}

export type BadgeListProps = { children: ReactNode; className?: string }

export function BadgeList({ children, className }: BadgeListProps) {
  return <div className={cn('flex flex-wrap items-center gap-1.5', className)}>{children}</div>
}
