import type { ComponentPropsWithRef, ReactNode } from 'react'

import { cn } from '@/utils/cn'

export type CardVariant =
  /** A bordered surface. The default for panels that sit on the canvas. */
  | 'plain'
  /** Adds ambient elevation. For anything that floats over content. */
  | 'raised'
  /** ADR-010's double-bezel: a tray, then a pane inside it. Reserved for the
   *  few containers meant to read as objects rather than as regions — a
   *  dashboard's hero metric, an auth card. Using it everywhere flattens the
   *  hierarchy it exists to create. */
  | 'bezel'

export type CardProps = ComponentPropsWithRef<'div'> & {
  variant?: CardVariant
}

export function Card({ variant = 'plain', className, children, ...rest }: CardProps) {
  if (variant === 'bezel') {
    return (
      <div className={cn('bezel', className)} {...rest}>
        <div className="bezel-core">{children}</div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'rounded-xl border bg-surface',
        variant === 'raised' ? 'border-border shadow-md' : 'border-border-subtle',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  )
}

export function CardHeader({ className, children, ...rest }: ComponentPropsWithRef<'div'>) {
  return (
    <div
      className={cn('flex items-start justify-between gap-4 px-4 pt-4 pb-3', className)}
      {...rest}
    >
      {children}
    </div>
  )
}

export function CardTitle({ className, children, ...rest }: ComponentPropsWithRef<'h2'>) {
  return (
    <h2 className={cn('text-sm font-semibold text-ink', className)} {...rest}>
      {children}
    </h2>
  )
}

export function CardDescription({ className, children, ...rest }: ComponentPropsWithRef<'p'>) {
  return (
    <p className={cn('text-[0.8125rem] text-ink-muted', className)} {...rest}>
      {children}
    </p>
  )
}

export function CardContent({ className, children, ...rest }: ComponentPropsWithRef<'div'>) {
  return (
    <div className={cn('px-4 pb-4', className)} {...rest}>
      {children}
    </div>
  )
}

/** A card's action row. Separated by a hairline rather than a gap, so a footer
 *  reads as attached to what is above it rather than as a new section. */
export function CardFooter({ className, children, ...rest }: ComponentPropsWithRef<'div'>) {
  return (
    <div
      className={cn(
        'flex items-center justify-end gap-2 border-t border-border-subtle px-4 py-3',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  )
}

export type PageHeaderProps = {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  className?: string
}

/**
 * The heading block every product screen opens with.
 *
 * One component rather than a per-page arrangement of an `h1` and a flex row,
 * because the alternative is four pages that each decided slightly differently
 * how much space goes below the title — and the mismatch only shows when
 * someone tabs between them.
 */
export function PageHeader({ title, description, actions, className }: PageHeaderProps) {
  return (
    <div className={cn('flex flex-wrap items-start justify-between gap-4', className)}>
      <div className="flex min-w-0 flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-[-0.02em] text-ink">{title}</h1>
        {description && <p className="text-[0.8125rem] text-ink-muted">{description}</p>}
      </div>

      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}
