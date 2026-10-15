import type { ComponentPropsWithRef, ReactNode } from 'react'

import { cn } from '@/utils/cn'

import {
  CONTROL_CLASSES,
  CONTROL_WITH_LEADING_ICON_CLASSES,
  CONTROL_WITH_TRAILING_AFFORDANCE_CLASSES,
} from './control-classes'
import { useFieldControl } from './field-context'

export type InputProps = Omit<ComponentPropsWithRef<'input'>, 'size'> & {
  leadingIcon?: ReactNode
  /** Affordance rendered at the right edge — a clear button on a search field,
   *  say. Positioned by the wrapper, not the caller. */
  trailingAffordance?: ReactNode
}

export function Input({
  className,
  leadingIcon,
  trailingAffordance,
  id,
  ...rest
}: InputProps) {
  const field = useFieldControl()
  const resolvedId = id ?? field?.id

  const control = (
    <input
      id={resolvedId}
      aria-describedby={field?.describedBy}
      aria-invalid={field?.invalid || undefined}
      aria-required={field?.required || undefined}
      className={cn(
        CONTROL_CLASSES,
        'h-9 rounded-lg px-3 text-sm',
        // Ternaries rather than `&&`: `leadingIcon` is a `ReactNode`, which
        // includes `number`, so `leadingIcon && 'pl-9'` types as `0 | 'pl-9'`
        // and `cn` would be handed a number.
        leadingIcon ? CONTROL_WITH_LEADING_ICON_CLASSES : null,
        trailingAffordance ? CONTROL_WITH_TRAILING_AFFORDANCE_CLASSES : null,
        className,
      )}
      {...rest}
    />
  )

  // No wrapper when there is nothing to position: an extra div around every
  // plain input would break flex and grid layouts that expect the input itself
  // to be the child.
  if (!leadingIcon && !trailingAffordance) {
    return control
  }

  return (
    <div className="relative flex items-center">
      {leadingIcon && (
        // pointer-events-none so the icon cannot swallow a click meant for the
        // input behind it.
        <span className="pointer-events-none absolute left-3 flex text-ink-subtle [&_svg]:size-4">
          {leadingIcon}
        </span>
      )}

      {control}

      {trailingAffordance && (
        <span className="absolute right-2 flex items-center">{trailingAffordance}</span>
      )}
    </div>
  )
}
