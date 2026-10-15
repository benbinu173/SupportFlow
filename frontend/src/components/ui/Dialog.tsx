import { X } from '@phosphor-icons/react'
import { useEffect, useId, useRef, type ReactNode } from 'react'

import { cn } from '@/utils/cn'

import { Button } from './Button'
import { IconButton } from './IconButton'

const SIZE_CLASSES = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
} as const

export type DialogSize = keyof typeof SIZE_CLASSES

export type DialogProps = {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  children?: ReactNode
  footer?: ReactNode
  size?: DialogSize
  /** Set false for a destructive confirmation that must be answered with a
   *  button. Escape and the backdrop both stop closing it. */
  dismissible?: boolean
}

/**
 * A modal, on the native `<dialog>` element.
 *
 * The element is doing the hard part: `showModal()` gives focus trapping, an
 * inert background, Escape-to-close, and the top layer — none of which is
 * reimplemented here, and all of which is routinely got subtly wrong by a
 * portal-based modal. This component's only jobs are keeping React state and
 * the element's own `open` in step, and styling it.
 *
 * The enter and exit transitions live in `index.css`, because they depend on
 * `allow-discrete` — see the note there.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  dismissible = true,
}: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const descriptionId = useId()

  useEffect(() => {
    const dialog = ref.current

    if (!dialog) {
      return
    }

    if (open && !dialog.open) {
      dialog.showModal()
    } else if (!open && dialog.open) {
      dialog.close()
    }
  }, [open])

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      {...(description ? { 'aria-describedby': descriptionId } : {})}
      // Escape and a `method="dialog"` form close the element directly, without
      // going through React. Mirroring that here is what stops the element's
      // state and ours from disagreeing after a keyboard dismissal.
      onClose={onClose}
      onCancel={(event) => {
        if (!dismissible) {
          event.preventDefault()
        }
      }}
      // A backdrop click reports the dialog itself as the target, because the
      // backdrop is a pseudo-element of it. All content lives in the wrapper
      // below, so the dialog's own box is only ever the backdrop.
      onClick={(event) => {
        if (dismissible && event.target === ref.current) {
          onClose()
        }
      }}
      className={cn(
        'm-auto max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] overflow-hidden',
        'rounded-2xl border border-border bg-surface p-0 text-ink shadow-xl',
        SIZE_CLASSES[size],
      )}
    >
      <div className="flex max-h-[calc(100dvh-2rem)] flex-col">
        <header className="flex items-start justify-between gap-4 px-5 pt-5 pb-4">
          <div className="flex min-w-0 flex-col gap-1">
            <h2 id={titleId} className="text-base font-semibold text-ink">
              {title}
            </h2>
            {description && (
              <p id={descriptionId} className="text-[0.8125rem] leading-relaxed text-ink-muted">
                {description}
              </p>
            )}
          </div>

          {dismissible && (
            <IconButton
              aria-label="Close"
              size="sm"
              icon={<X />}
              onClick={onClose}
              className="-mt-1 -mr-1"
            />
          )}
        </header>

        {children && <div className="min-h-0 overflow-y-auto px-5 pb-5">{children}</div>}

        {footer && (
          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-border-subtle bg-surface-sunken px-5 py-3.5">
            {footer}
          </div>
        )}
      </div>
    </dialog>
  )
}

export type ConfirmDialogProps = {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: ReactNode
  description?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  /** `danger` for anything that destroys something. The default is not
   *  `danger`, because a confirm dialog that always shouts stops being read. */
  variant?: 'primary' | 'danger'
  loading?: boolean
}

/**
 * The "are you sure" dialog §39 asks for, so that no screen has to reassemble
 * it. Keeping the buttons in a fixed order and the danger variant in one place
 * is what makes destructive actions feel consistent rather than merely present.
 */
export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'primary',
  loading = false,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      {...(description ? { description } : {})}
      size="sm"
      // A confirmation that can be dismissed by pressing Escape on the way to
      // the button it was warning about is not a confirmation.
      dismissible={!loading}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={loading}>
            {cancelLabel}
          </Button>
          <Button variant={variant} onClick={onConfirm} loading={loading}>
            {confirmLabel}
          </Button>
        </>
      }
    />
  )
}
