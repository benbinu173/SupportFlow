import { useId, type ComponentPropsWithRef, type ReactNode } from 'react'

import { cn } from '@/utils/cn'

import { useFieldControl } from './field-context'

export type SwitchProps = Omit<ComponentPropsWithRef<'input'>, 'type' | 'role'> & {
  label: ReactNode
  description?: ReactNode
}

/**
 * An on/off control for a setting that takes effect immediately.
 *
 * A checkbox with `role="switch"` rather than a button with `aria-checked`:
 * the native element already answers Space, already reports its state, and
 * already works inside a form. The visual track and thumb are drawn with
 * `::after` on the restyled input — an `appearance: none` checkbox is a normal
 * box as far as pseudo-elements are concerned.
 *
 * The distinction from `Checkbox` is not decorative. A checkbox means "include
 * this in what I am about to submit"; a switch means "this is now on". Using
 * the wrong one is why settings pages feel unpredictable.
 *
 * Like `Checkbox`, the description is linked rather than wrapped — see the note
 * there on why a hint inside the label corrupts the accessible name.
 */
export function Switch({ label, description, className, id, ...rest }: SwitchProps) {
  const generatedId = useId()
  const field = useFieldControl()
  const resolvedId = id ?? field?.id ?? generatedId
  const descriptionId = `${generatedId}-description`

  const describedBy =
    [field?.describedBy, description ? descriptionId : undefined].filter(Boolean).join(' ') ||
    undefined

  return (
    <div className="flex flex-col gap-0.5">
      <label htmlFor={resolvedId} className="group flex cursor-pointer items-start gap-3">
        <input
          type="checkbox"
          role="switch"
          id={resolvedId}
          aria-describedby={describedBy}
          className={cn(
            'relative mt-0.5 h-5 w-9 shrink-0 cursor-pointer appearance-none rounded-full bg-surface-active',
            'border border-transparent',
            // The thumb. Leaving the transition on `transform` alone keeps the
            // animation on the compositor.
            'after:absolute after:top-0.5 after:left-0.5 after:size-3.5',
            'after:rounded-full after:bg-white after:shadow-sm',
            'after:transition-transform after:duration-200 after:ease-spring',
            'transition-colors duration-200 ease-out-expo',
            'checked:bg-brand checked:after:translate-x-4',
            'focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-brand/20',
            'disabled:cursor-not-allowed disabled:opacity-50',
            className,
          )}
          {...rest}
        />

        <span className="text-sm leading-5 text-ink">{label}</span>
      </label>

      {/* Indented past the track (`w-9`) and the gap (`gap-3`). */}
      {description && (
        <p id={descriptionId} className="pl-12 text-[0.8125rem] text-ink-muted">
          {description}
        </p>
      )}
    </div>
  )
}
