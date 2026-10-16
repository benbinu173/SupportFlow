import type { CSSProperties, ReactNode } from 'react'

import { cn } from '@/utils/cn'

/**
 * A block that arrives when it mounts.
 *
 * The mount-time counterpart to `Reveal`, and the distinction is the reason
 * both exist: `Reveal` waits for an `IntersectionObserver` and belongs to the
 * marketing surface, where the page is longer than the viewport and the
 * interesting moment is a section crossing into view. An auth screen is one
 * viewport with everything already on it, so there is nothing to observe — the
 * choreography is only the order the pieces appear in.
 *
 * **CSS rather than `motion`, for three reasons that point the same way.** A
 * mount entry is a fixed sequence with nothing to interpolate between frames,
 * so it needs no runtime; a CSS animation is handed to the compositor rather
 * than spending the main thread's frame budget; and the `prefers-reduced-motion`
 * rule in `index.css` already collapses it, so it needs no second escape hatch
 * in JavaScript. `motion` still animates the panel's parallax, which is
 * pointer-driven and has no CSS equivalent.
 *
 * **The test consequence of the inline-style version is the argument that
 * settles it.** `motion` writes its `initial` as an inline `opacity: 0`, and
 * jsdom reads inline styles — so every element wrapped for an entrance became
 * invisible to `toBeVisible()` in a test that had nothing to do with motion.
 * A keyframe is invisible to jsdom's computed styles, which is the right
 * outcome: a test asserting that a heading is on screen should not be coupled
 * to how long it takes to fade in.
 *
 * Only `opacity` and `transform` are animated, per ADR-010's guardrail — the
 * two properties a browser can composite without a layout pass.
 *
 * `y` defaults low, and a caller putting this inside a `bezel` should keep it
 * low: `bezel-core` is `overflow: hidden`, so an element that starts far enough
 * down is clipped during the first frames of its own entrance.
 */
export function Enter({
  children,
  delay = 0,
  y = 12,
  className,
}: {
  children: ReactNode
  /** Milliseconds, matching the `--enter-delay` this feeds. Stated at the call
   *  site rather than derived from an index, because on a screen this short the
   *  order is the choreography and it should be readable in one place. */
  delay?: number
  y?: number
  className?: string
}) {
  return (
    <div
      className={cn('animate-enter', className)}
      style={
        {
          '--enter-delay': `${String(delay)}ms`,
          '--enter-y': `${String(y)}px`,
        } as CSSProperties
      }
    >
      {children}
    </div>
  )
}
