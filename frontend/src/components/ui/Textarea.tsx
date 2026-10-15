import type { ComponentPropsWithRef } from 'react'

import { cn } from '@/utils/cn'

import { CONTROL_CLASSES } from './control-classes'
import { useFieldControl } from './field-context'

export type TextareaProps = ComponentPropsWithRef<'textarea'>

export function Textarea({ className, id, rows = 4, ...rest }: TextareaProps) {
  const field = useFieldControl()

  return (
    <textarea
      id={id ?? field?.id}
      rows={rows}
      aria-describedby={field?.describedBy}
      aria-invalid={field?.invalid || undefined}
      aria-required={field?.required || undefined}
      className={cn(
        CONTROL_CLASSES,
        // `resize-y` rather than the browser default `both`: a reply box that
        // can be dragged wider than its column breaks the two-column layout
        // §40 specifies, and nothing useful is gained by allowing it.
        'resize-y rounded-lg px-3 py-2 text-sm leading-relaxed',
        className,
      )}
      {...rest}
    />
  )
}
