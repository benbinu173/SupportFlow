import type { ComponentPropsWithRef, ReactNode } from 'react'

import { cn } from '@/utils/cn'

import { type ButtonSize, type ButtonVariant } from './button-classes'
import { Spinner } from './Spinner'

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary: 'bg-brand text-brand-fg shadow-sm hover:bg-brand-hover active:bg-brand-active',
  secondary:
    'border border-border bg-surface text-ink shadow-xs hover:bg-surface-hover active:bg-surface-active',
  subtle: 'bg-surface-sunken text-ink hover:bg-surface-hover active:bg-surface-active',
  ghost: 'text-ink-muted hover:bg-surface-hover hover:text-ink active:bg-surface-active',
  danger: 'bg-danger text-danger-fg shadow-sm hover:bg-danger-hover active:bg-danger-active',
  'danger-ghost': 'text-danger-text hover:bg-danger-subtle active:bg-danger-subtle',
}

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'size-8 rounded-md',
  md: 'size-9 rounded-lg',
  lg: 'size-11 rounded-xl',
}

const ICON_SIZE: Record<ButtonSize, 'sm' | 'md' | 'lg'> = {
  sm: 'sm',
  md: 'md',
  lg: 'lg',
}

export type IconButtonProps = Omit<ComponentPropsWithRef<'button'>, 'aria-label' | 'children'> & {
  /** Required, not optional. An icon has no accessible name of its own, so a
   *  missing label is an unlabelled control rather than a lint warning. */
  'aria-label': string
  icon: ReactNode
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
}

export function IconButton({
  icon,
  variant = 'ghost',
  size = 'md',
  loading = false,
  className,
  disabled,
  type = 'button',
  ...rest
}: IconButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(
        'inline-flex shrink-0 select-none items-center justify-center',
        'transition-[color,background-color,border-color,box-shadow,transform]',
        'duration-150 ease-out-expo',
        'active:scale-[0.94]',
        'disabled:pointer-events-none disabled:opacity-50',
        '[&_svg]:shrink-0',
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...rest}
    >
      {loading ? <Spinner size={ICON_SIZE[size]} /> : icon}
    </button>
  )
}
