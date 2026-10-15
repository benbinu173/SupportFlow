import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

import { cn } from '@/utils/cn'

export type TooltipSide = 'top' | 'bottom' | 'left' | 'right'

const SIDE_CLASSES: Record<TooltipSide, string> = {
  top: 'bottom-full left-1/2 -translate-x-1/2 mb-2',
  bottom: 'top-full left-1/2 -translate-x-1/2 mt-2',
  left: 'right-full top-1/2 -translate-y-1/2 mr-2',
  right: 'left-full top-1/2 -translate-y-1/2 ml-2',
}

// The arrow, counter-rotated per side so it stays a diamond pointing at the
// trigger rather than becoming a stray square.
const ARROW_CLASSES: Record<TooltipSide, string> = {
  top: 'top-full left-1/2 -translate-x-1/2 -mt-1',
  bottom: 'bottom-full left-1/2 -translate-x-1/2 -mb-1',
  left: 'left-full top-1/2 -translate-y-1/2 -ml-1',
  right: 'right-full top-1/2 -translate-y-1/2 -mr-1',
}

export type TooltipProps = {
  content: ReactNode
  children: ReactNode
  side?: TooltipSide
  /** Milliseconds of hovering before it appears. Long enough that sweeping the
   *  pointer across a toolbar produces nothing, short enough not to feel
   *  broken when the pointer stops. */
  delay?: number
  className?: string
}

/**
 * A label on hover and on focus.
 *
 * Dependency-free, and deliberately without collision detection: it is
 * positioned with plain CSS relative to its wrapper, so a tooltip near a
 * clipped edge will be clipped. That is an acceptable trade for the toolbars
 * and icon buttons this is actually used on, all of which sit well inside their
 * containers — auto-flipping would mean pulling in a positioning engine for a
 * case the product does not have. Worth revisiting if a screen ever needs it.
 *
 * It does not wire `aria-describedby` to the trigger. Every trigger in this
 * codebase already carries its own accessible name (`IconButton` requires one),
 * and a description that repeats the name is announced twice. Where a tooltip
 * will carry information the trigger does not, wire the description by hand.
 */
export function Tooltip({ content, children, side = 'top', delay = 400, className }: TooltipProps) {
  const [open, setOpen] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const id = useId()

  const clearTimer = () => {
    if (timer.current !== null) {
      clearTimeout(timer.current)
      timer.current = null
    }
  }

  const show = (immediate = false) => {
    clearTimer()

    if (immediate) {
      setOpen(true)
      return
    }

    timer.current = setTimeout(() => {
      setOpen(true)
    }, delay)
  }

  const hide = () => {
    clearTimer()
    setOpen(false)
  }

  // A tooltip that outlives its unmount leaves a timer holding a setState on a
  // component that is gone.
  useEffect(() => clearTimer, [])

  useEffect(() => {
    if (!open) {
      return
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        hide()
      }
    }

    document.addEventListener('keydown', onKeyDown)

    return () => {
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <span
      className={cn('relative inline-flex', className)}
      onPointerEnter={() => {
        show()
      }}
      onPointerLeave={hide}
      // Focus is not hover: a keyboard user gets the label immediately, because
      // they have already committed to the control.
      onFocus={() => {
        show(true)
      }}
      onBlur={hide}
    >
      {children}

      {open && (
        <span
          id={id}
          role="tooltip"
          className={cn(
            'pointer-events-none absolute z-tooltip w-max max-w-64 rounded-lg px-2.5 py-1.5',
            'bg-surface-inverse text-xs font-medium text-ink-inverse shadow-lg',
            // Opacity only, and only on entry: the element is positioned with a
            // centring translate, and an exit animation would need it to
            // survive its own removal from the tree.
            'animate-fade-in',
            SIDE_CLASSES[side],
          )}
        >
          {content}
          <span
            aria-hidden="true"
            className={cn('absolute size-2 rotate-45 bg-surface-inverse', ARROW_CLASSES[side])}
          />
        </span>
      )}
    </span>
  )
}
