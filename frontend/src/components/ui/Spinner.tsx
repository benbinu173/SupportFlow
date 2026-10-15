import type { ComponentPropsWithRef } from 'react'

import { cn } from '@/utils/cn'

export type SpinnerSize = 'xs' | 'sm' | 'md' | 'lg'

const SIZE_CLASSES: Record<SpinnerSize, string> = {
  xs: 'size-3',
  sm: 'size-3.5',
  md: 'size-4',
  lg: 'size-5',
}

export type SpinnerProps = Omit<ComponentPropsWithRef<'svg'>, 'children'> & {
  size?: SpinnerSize
  /**
   * Announced to screen readers. Omit it where the spinner sits inside a
   * control that already reports `aria-busy` — two live regions describing one
   * wait is noise, not redundancy.
   */
  label?: string
}

export function Spinner({ size = 'md', label, className, ...rest }: SpinnerProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      className={cn('animate-spin', SIZE_CLASSES[size], className)}
      {...(label ? { role: 'status', 'aria-label': label } : { 'aria-hidden': true })}
      {...rest}
    >
      {/* A full track plus a quarter arc. The track is what stops the spinner
          from looking like a broken bracket at small sizes. */}
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" className="opacity-20" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  )
}
