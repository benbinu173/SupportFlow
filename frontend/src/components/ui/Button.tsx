import type { ComponentPropsWithRef, ReactNode } from 'react'

import { cn } from '@/utils/cn'

import { GAP_CLASSES, buttonClasses, type ButtonSize, type ButtonVariant } from './button-classes'
import { Spinner } from './Spinner'

export type ButtonProps = ComponentPropsWithRef<'button'> & {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  leadingIcon?: ReactNode
  trailingIcon?: ReactNode
  fullWidth?: boolean
}

export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  leadingIcon,
  trailingIcon,
  fullWidth = false,
  className,
  children,
  disabled,
  // Defaults to `button`, not the HTML default of `submit`. A stray button in a
  // form silently submitting it is a bug that only shows up in the one form
  // that forgot to pass `type="submit"`.
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={buttonClasses({ variant, size, fullWidth, ...(className ? { className } : {}) })}
      {...rest}
    >
      {/* The label stays mounted and is hidden with `invisible` rather than
          removed: unmounting it would collapse the button to the spinner's
          width, and a button that resizes mid-click moves the thing the user
          was aiming at. */}
      <span
        className={cn(
          'inline-flex items-center justify-center',
          GAP_CLASSES[size],
          loading && 'invisible',
        )}
      >
        {leadingIcon}
        {children}
        {trailingIcon}
      </span>

      {loading && (
        <span className="absolute inset-0 grid place-items-center">
          <Spinner size={size === 'lg' ? 'md' : 'sm'} />
        </span>
      )}
    </button>
  )
}
