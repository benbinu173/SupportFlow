import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react'

import { cn } from '@/utils/cn'

export interface DropdownMenuItem {
  id: string
  label: ReactNode
  icon?: ReactNode
  onSelect: () => void
  /** Renders in the danger colour. For destructive items. */
  danger?: boolean
  disabled?: boolean
  /** Draws a hairline above this item, grouping it away from those before it. */
  separatorBefore?: boolean
  /** Right-aligned and muted — a count, or a keyboard hint. */
  meta?: ReactNode
}

export interface DropdownMenuProps {
  /** The accessible name of the menu and of the button that opens it. */
  label: string
  trigger: ReactNode
  items: DropdownMenuItem[]
  align?: 'start' | 'end'
  side?: 'bottom' | 'top'
  triggerClassName?: string
  className?: string
}

/**
 * A menu of actions, anchored to a trigger.
 *
 * Implements the WAI-ARIA menu pattern properly rather than approximating it:
 * roving `tabIndex` so a keyboard user is never tabbing through every item,
 * arrow keys that skip disabled entries, Home/End, Escape that closes and
 * returns focus to the trigger, and Tab that closes without trapping.
 *
 * Focus returns to the trigger on every close path except a click elsewhere —
 * stealing focus back from wherever the user just clicked is worse than
 * leaving it.
 */
export function DropdownMenu({
  label,
  trigger,
  items,
  align = 'end',
  side = 'bottom',
  triggerClassName,
  className,
}: DropdownMenuProps) {
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)

  const containerRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])

  const enabledIndexes = useMemo(
    () =>
      items.reduce<number[]>((indexes, item, index) => {
        if (!item.disabled) {
          indexes.push(index)
        }

        return indexes
      }, []),
    [items],
  )

  const close = useCallback(
    (refocus = true) => {
      setOpen(false)

      if (refocus) {
        triggerRef.current?.focus()
      }
    },
    [],
  )

  // Opening lands on the first selectable item, not merely the first item.
  //
  // Set from the events that open the menu rather than from an effect watching
  // `open`. Beyond the extra render pass, an effect here has a real bug in it:
  // `enabledIndexes` is derived from the `items` prop, so any re-render of the
  // parent while the menu is open — a store update elsewhere in the topbar,
  // say — would re-run the effect and yank the highlight back to the top,
  // mid-arrow-key. Setting it at the moment of opening cannot do that.
  const openMenu = useCallback(() => {
    setActiveIndex(enabledIndexes[0] ?? 0)
    setOpen(true)
  }, [enabledIndexes])

  useEffect(() => {
    if (open) {
      itemRefs.current[activeIndex]?.focus()
    }
  }, [open, activeIndex])

  useEffect(() => {
    if (!open) {
      return
    }

    const onPointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        // No refocus: the user chose somewhere else to be.
        close(false)
      }
    }

    document.addEventListener('pointerdown', onPointerDown)

    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
    }
  }, [open, close])

  const moveTo = (direction: 1 | -1) => {
    if (enabledIndexes.length === 0) {
      return
    }

    const position = enabledIndexes.indexOf(activeIndex)

    // Wrapping, both ways. A menu whose first item cannot be reached by
    // pressing Up is a menu people stop using the keyboard on.
    const nextPosition =
      position === -1
        ? 0
        : (position + direction + enabledIndexes.length) % enabledIndexes.length

    setActiveIndex(enabledIndexes[nextPosition] ?? activeIndex)
  }

  const handleKeyDown = (event: KeyboardEvent) => {
    if (!open) {
      if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        openMenu()
      }

      return
    }

    switch (event.key) {
      case 'Escape':
        event.preventDefault()
        close()
        break

      case 'Tab':
        // Closes without preventing the default, so focus moves on naturally
        // rather than being trapped in a menu the user is done with.
        close(false)
        break

      case 'ArrowDown':
        event.preventDefault()
        moveTo(1)
        break

      case 'ArrowUp':
        event.preventDefault()
        moveTo(-1)
        break

      case 'Home':
        event.preventDefault()
        setActiveIndex(enabledIndexes[0] ?? 0)
        break

      case 'End':
        event.preventDefault()
        setActiveIndex(enabledIndexes[enabledIndexes.length - 1] ?? 0)
        break

      default:
        break
    }
  }

  const select = (item: DropdownMenuItem) => {
    if (item.disabled) {
      return
    }

    close()
    item.onSelect()
  }

  return (
    <div ref={containerRef} className={cn('relative', className)} onKeyDown={handleKeyDown}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        onClick={() => {
          if (open) {
            setOpen(false)
          } else {
            openMenu()
          }
        }}
        className={triggerClassName}
      >
        {trigger}
      </button>

      {open && (
        <div
          role="menu"
          aria-label={label}
          className={cn(
            'absolute z-dropdown min-w-52 rounded-xl border border-border bg-surface p-1 shadow-lg',
            'animate-fade-in',
            side === 'top' ? 'bottom-full mb-1.5' : 'top-full mt-1.5',
            align === 'end' ? 'right-0' : 'left-0',
          )}
        >
          {items.map((item, index) => (
            <Fragment key={item.id}>
              {item.separatorBefore && (
                <div role="separator" className="my-1 h-px bg-border-subtle" />
              )}

              <button
                ref={(element) => {
                  itemRefs.current[index] = element
                }}
                type="button"
                role="menuitem"
                tabIndex={index === activeIndex ? 0 : -1}
                disabled={item.disabled}
                onClick={() => {
                  select(item)
                }}
                className={cn(
                  'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left text-sm',
                  'transition-colors duration-100 ease-out-expo',
                  'disabled:pointer-events-none disabled:opacity-50',
                  '[&_svg]:size-4 [&_svg]:shrink-0',
                  item.danger
                    ? 'text-danger-text hover:bg-danger-subtle'
                    : 'text-ink hover:bg-surface-hover',
                )}
              >
                {item.icon && <span className="text-ink-subtle">{item.icon}</span>}
                <span className="flex-1 truncate">{item.label}</span>
                {item.meta && <span className="text-2xs text-ink-subtle">{item.meta}</span>}
              </button>
            </Fragment>
          ))}
        </div>
      )}
    </div>
  )
}
