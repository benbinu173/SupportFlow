import { useId, type ReactNode } from 'react'

import { cn } from '@/utils/cn'

import { FieldContext } from './field-context'

export type FieldProps = {
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  required?: boolean
  /** Labels the control for assistive technology without showing it — for a
   *  filter bar where the placeholder already says what the control is. */
  labelHidden?: boolean
  className?: string
  children: ReactNode
}

/**
 * Label, optional hint, optional error, and the wiring that connects them to
 * one control.
 *
 * The wiring is the point. `htmlFor`, `aria-describedby` and `aria-invalid` are
 * handed to the control through context rather than typed at each call site, so
 * a form cannot ship an input whose error message exists on screen and nowhere
 * in the accessibility tree — which is the default outcome of doing it by hand.
 */
export function Field({
  label,
  hint,
  error,
  required = false,
  labelHidden = false,
  className,
  children,
}: FieldProps) {
  const id = useId()
  const hintId = `${id}-hint`
  const errorId = `${id}-error`

  // The error replaces the hint rather than being appended to it. A control
  // described by "must be at least 8 characters" and "this email is already
  // registered" at the same time is being told two things, one of which is no
  // longer true.
  const describedBy = error ? errorId : hint ? hintId : undefined

  return (
    <FieldContext value={{ id, describedBy, invalid: Boolean(error), required }}>
      <div className={cn('flex flex-col gap-1.5', className)}>
        <label
          htmlFor={id}
          className={cn('text-sm font-medium text-ink', labelHidden && 'sr-only')}
        >
          {label}
          {required && (
            // Decorative: `aria-required` on the control is what announces it.
            // A bare asterisk read aloud is punctuation, not information.
            <span aria-hidden="true" className="ml-0.5 text-danger-text">
              *
            </span>
          )}
        </label>

        {children}

        {/* `aria-live` on the error only. A hint appearing is not an event; an
            error appearing is the thing the user needs to hear. */}
        {error ? (
          <p id={errorId} aria-live="polite" className="text-[0.8125rem] text-danger-text">
            {error}
          </p>
        ) : hint ? (
          <p id={hintId} className="text-[0.8125rem] text-ink-muted">
            {hint}
          </p>
        ) : null}
      </div>
    </FieldContext>
  )
}
