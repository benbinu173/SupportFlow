import { CheckCircle, Info, Warning, WarningCircle, X } from '@phosphor-icons/react'
import { AnimatePresence, motion } from 'motion/react'
import type { ReactNode } from 'react'

import { useToastStore, type ToastVariant } from '@/store/toasts'
import { cn } from '@/utils/cn'

import { IconButton } from './IconButton'

const VARIANT_CLASSES: Record<ToastVariant, string> = {
  info: 'border-info-border bg-info-subtle text-info-text',
  success: 'border-success-border bg-success-subtle text-success-text',
  warning: 'border-warning-border bg-warning-subtle text-warning-text',
  danger: 'border-danger-border bg-danger-subtle text-danger-text',
}

const VARIANT_ICONS: Record<ToastVariant, ReactNode> = {
  info: <Info aria-hidden="true" />,
  success: <CheckCircle aria-hidden="true" />,
  warning: <Warning aria-hidden="true" />,
  danger: <WarningCircle aria-hidden="true" />,
}

/**
 * The toast stack.
 *
 * Mounted once, near the root. Toasts are pushed from anywhere — a mutation's
 * `onError`, an axios interceptor, a WebSocket handler — through the `toast`
 * helpers in `@/store/toasts`, which is why this owns the rendering and nothing
 * else does.
 *
 * `motion` rather than a CSS transition, because exit animations need an
 * element to survive its own removal from the list and CSS has no way to do
 * that. `MotionConfig reducedMotion="user"` at the app root is what makes the
 * whole library obey `prefers-reduced-motion`.
 */
export function Toaster() {
  const toasts = useToastStore((state) => state.toasts)
  const dismiss = useToastStore((state) => state.dismiss)

  return (
    <div
      // The container is inert and each card carries its own live-region role,
      // rather than the container being one region: a single region would
      // re-announce the entire stack every time one toast was dismissed.
      className="pointer-events-none fixed right-4 bottom-4 z-toast flex w-[calc(100vw-2rem)] max-w-sm flex-col gap-2"
    >
      <AnimatePresence initial={false}>
        {toasts.map((item) => (
          <motion.div
            key={item.id}
            layout
            initial={{ opacity: 0, y: 12, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, scale: 0.97, transition: { duration: 0.15 } }}
            // The custom cubic-bezier from the token set, inline because motion
            // animates in JS and cannot read a CSS variable.
            transition={{ duration: 0.24, ease: [0.32, 0.72, 0, 1] }}
            // `alert` interrupts; `status` waits its turn. A failed save is
            // worth interrupting for, a successful one is not.
            role={item.variant === 'danger' || item.variant === 'warning' ? 'alert' : 'status'}
            className={cn(
              'pointer-events-auto flex items-start gap-3 rounded-xl border p-3 shadow-lg',
              VARIANT_CLASSES[item.variant],
            )}
          >
            <span className="mt-0.5 shrink-0 [&_svg]:size-4">{VARIANT_ICONS[item.variant]}</span>

            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <p className="text-sm font-medium">{item.title}</p>
              {item.description && (
                <p className="text-[0.8125rem] leading-relaxed opacity-90">{item.description}</p>
              )}
            </div>

            <IconButton
              aria-label="Dismiss"
              size="sm"
              icon={<X />}
              onClick={() => {
                dismiss(item.id)
              }}
              className="-mt-1 -mr-1 shrink-0 text-current hover:bg-black/5 dark:hover:bg-white/10"
            />
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  )
}
