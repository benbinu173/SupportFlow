import { cn } from '@/utils/cn'

/**
 * The button's class vocabulary, apart from the button.
 *
 * Split out for the same reason `control-classes.ts` is: these are the styles,
 * and `buttonClasses` exists so a router `<Link>` can wear them without being
 * wrapped in a `<button>`. A component module that also exports a
 * non-component stops being fast-refreshable, and the alternative — telling
 * callers to reach into the component file for a string — is worse.
 */

export type ButtonVariant =
  | 'primary'
  | 'secondary'
  | 'subtle'
  | 'ghost'
  | 'danger'
  | 'danger-ghost'

export type ButtonSize = 'sm' | 'md' | 'lg'

export const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary: 'bg-brand text-brand-fg shadow-sm hover:bg-brand-hover active:bg-brand-active',
  secondary:
    'border border-border bg-surface text-ink shadow-xs hover:bg-surface-hover active:bg-surface-active',
  subtle: 'bg-surface-sunken text-ink hover:bg-surface-hover active:bg-surface-active',
  ghost: 'text-ink-muted hover:bg-surface-hover hover:text-ink active:bg-surface-active',
  danger: 'bg-danger text-danger-fg shadow-sm hover:bg-danger-hover active:bg-danger-active',
  'danger-ghost': 'text-danger-text hover:bg-danger-subtle active:bg-danger-subtle',
}

/* Radius grows with height rather than staying fixed, so a `lg` button does not
   read as a rounded rectangle. The product surfaces use md–xl; marketing
   passes `rounded-full` for the pill shape ADR-010 reserves for CTAs. */
export const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'h-8 rounded-md px-3 text-[0.8125rem]',
  md: 'h-9 rounded-lg px-3.5 text-sm',
  lg: 'h-11 rounded-xl px-5 text-[0.9375rem]',
}

export const GAP_CLASSES: Record<ButtonSize, string> = {
  sm: 'gap-1.5',
  md: 'gap-2',
  lg: 'gap-2',
}

export const BASE_CLASSES = [
  'relative inline-flex select-none items-center justify-center',
  'whitespace-nowrap font-medium',
  'transition-[color,background-color,border-color,box-shadow,transform]',
  'duration-150 ease-out-expo',
  // A press should feel like a press. 0.98 is enough to register and not
  // enough to read as a bounce.
  'active:scale-[0.98]',
  'disabled:pointer-events-none disabled:opacity-50',
  '[&_svg]:shrink-0',
].join(' ')

export interface ButtonClassOptions {
  variant?: ButtonVariant
  size?: ButtonSize
  fullWidth?: boolean
  className?: string
}

/**
 * The class list a button is made of, for the times the element cannot be a
 * `<button>`.
 *
 * A router link that looks like a button is the case this exists for. The
 * alternatives are both worse: an `<a>` wrapped around a `<button>` is invalid
 * HTML and announces itself as two controls, and a `<button>` that calls
 * `navigate()` loses middle-click, ctrl-click, and "open in new tab" — which on
 * a navigation is a real loss, not a nicety.
 */
export function buttonClasses({
  variant = 'primary',
  size = 'md',
  fullWidth = false,
  className,
}: ButtonClassOptions = {}): string {
  return cn(
    BASE_CLASSES,
    VARIANT_CLASSES[variant],
    SIZE_CLASSES[size],
    fullWidth && 'w-full',
    className,
  )
}
