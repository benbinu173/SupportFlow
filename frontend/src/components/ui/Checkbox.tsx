import { Check } from '@phosphor-icons/react'
import { useId, type ComponentPropsWithRef, type ReactNode } from 'react'

import { cn } from '@/utils/cn'

import { useFieldControl } from './field-context'

export type CheckboxProps = Omit<ComponentPropsWithRef<'input'>, 'type'> & {
  label: ReactNode
  description?: ReactNode
}

/**
 * A checkbox with a label that is not optional.
 *
 * The label is required because the alternative — a bare box with an
 * `aria-label` — pushes the same string into a place nobody reads, and the
 * visible text and the accessible name then drift. Here the visible text *is*
 * the accessible name: clicking the words toggles the box and a screen reader
 * reads them.
 *
 * The description is linked with `aria-describedby` and sits *outside* the
 * `<label>`, not inside it. Text within a label becomes part of the control's
 * accessible name, so a hint left in there turns "Auto-assign" into
 * "Auto-assign Round-robin across the team" — unskippable for a screen reader
 * user, and unmatchable for voice control saying the short phrase.
 */
export function Checkbox({ label, description, className, id, ...rest }: CheckboxProps) {
  const generatedId = useId()
  const field = useFieldControl()
  const resolvedId = id ?? field?.id ?? generatedId
  const descriptionId = `${generatedId}-description`

  const describedBy =
    [field?.describedBy, description ? descriptionId : undefined].filter(Boolean).join(' ') ||
    undefined

  return (
    <div className="flex flex-col gap-0.5">
      <label htmlFor={resolvedId} className="group flex cursor-pointer items-start gap-2.5">
        <span className="relative mt-0.5 grid size-4 shrink-0 place-items-center">
          <input
            type="checkbox"
            id={resolvedId}
            aria-describedby={describedBy}
            aria-invalid={field?.invalid || undefined}
            className={cn(
              'peer size-4 cursor-pointer appearance-none rounded-xs border border-border-strong bg-surface',
              'transition-[color,background-color,border-color,box-shadow] duration-150 ease-out-expo',
              'hover:border-ink-subtle',
              'checked:border-brand checked:bg-brand',
              'focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-brand/20',
              'disabled:cursor-not-allowed disabled:border-border disabled:bg-surface-sunken',
              'aria-invalid:border-danger',
              className,
            )}
            {...rest}
          />

          {/* A direct sibling of the input, because Tailwind's `peer-checked:`
              compiles to `:where(.peer):checked ~ *` and only reaches siblings —
              nesting this any deeper would silently stop it working. */}
          <Check
            aria-hidden="true"
            weight="bold"
            className="pointer-events-none absolute size-3 text-brand-fg opacity-0 transition-opacity duration-150 peer-checked:opacity-100 peer-disabled:opacity-40"
          />
        </span>

        <span className="text-sm leading-4 text-ink">{label}</span>
      </label>

      {/* Indented to the label's left edge: the box is `size-4` and the gap is
          `gap-2.5`, so 1.625rem puts the hint directly under the words. */}
      {description && (
        <p id={descriptionId} className="pl-6.5 text-[0.8125rem] text-ink-muted">
          {description}
        </p>
      )}
    </div>
  )
}
