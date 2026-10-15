import { CaretDown } from '@phosphor-icons/react'
import type { ComponentPropsWithRef } from 'react'

import { cn } from '@/utils/cn'

import { CONTROL_CLASSES, CONTROL_WITH_TRAILING_AFFORDANCE_CLASSES } from './control-classes'
import { useFieldControl } from './field-context'

export type SelectProps = ComponentPropsWithRef<'select'> & {
  /** Rendered as the first option with an empty value. Selecting it is
   *  equivalent to not having chosen, which is what makes an optional filter
   *  clearable without a separate reset control. */
  placeholder?: string
}

/**
 * A native `<select>`, restyled.
 *
 * Deliberately not a custom listbox. The platform control brings keyboard
 * navigation, typeahead, mobile pickers and screen-reader behaviour that a div
 * rebuilds badly — and the only thing genuinely wanted here is the chevron and
 * the border, both of which `appearance-none` plus a positioned icon provide.
 */
export function Select({ className, id, placeholder, children, ...rest }: SelectProps) {
  const field = useFieldControl()

  return (
    <div className="relative flex items-center">
      <select
        id={id ?? field?.id}
        aria-describedby={field?.describedBy}
        aria-invalid={field?.invalid || undefined}
        aria-required={field?.required || undefined}
        className={cn(
          CONTROL_CLASSES,
          CONTROL_WITH_TRAILING_AFFORDANCE_CLASSES,
          'h-9 cursor-pointer appearance-none rounded-lg pr-8 text-sm',
          // A select with no value selected shows this in the closed state, so
          // an unset filter reads as "Any" rather than as the first option.
          !rest.value && 'text-ink-subtle',
          className,
        )}
        {...rest}
      >
        {placeholder !== undefined && <option value="">{placeholder}</option>}
        {children}
      </select>

      <CaretDown
        aria-hidden="true"
        className="pointer-events-none absolute right-2.5 size-4 text-ink-subtle"
      />
    </div>
  )
}
