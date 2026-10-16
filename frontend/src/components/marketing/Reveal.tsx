import { motion } from 'motion/react'
import type { ReactNode } from 'react'

/**
 * A section that arrives as it is scrolled to.
 *
 * ADR-010 permits scroll-driven entry on the marketing surface and nowhere
 * else, and this is the single place that implements it. The product screens
 * get mount transitions only, because an agent triaging a queue must not watch
 * twenty rows fade in.
 *
 * `whileInView` rather than a scroll listener: motion's IntersectionObserver
 * fires twice per section instead of sixty times a second, which is the
 * difference between an effect and a continuous reflow. `once` means a section
 * does not re-animate every time it is scrolled past — an animation that
 * replays is a distraction on the second viewing and a stall on the tenth.
 *
 * Only `opacity` and `transform` are animated, per the guardrail: they are the
 * two properties a browser can composite without a layout pass.
 *
 * `MotionConfig reducedMotion="user"` in `App.tsx` drops the movement for
 * anyone who has asked their system for less of it, while leaving the fade —
 * which is the correct reading of the preference, not a workaround for it.
 */
export function Reveal({
  children,
  delay = 0,
  className,
}: {
  children: ReactNode
  delay?: number
  className?: string
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 28 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '-80px' }}
      transition={{ duration: 0.7, ease: [0.32, 0.72, 0, 1], delay }}
      className={className}
    >
      {children}
    </motion.div>
  )
}
