import { useMotionValue, useReducedMotion, useSpring } from 'motion/react'
import type { PointerEvent } from 'react'

/** How far the pointer can push a layer, in pixels, at the extremes of the
 *  panel. Small on purpose — this is depth, not dragging. */
const TRAVEL_X = 18
const TRAVEL_Y = 14

/**
 * A few pixels of parallax, driven by where the pointer is.
 *
 * **Motion values rather than state, and that is the entire reason this is
 * affordable.** `useMotionValue` writes a transform without re-rendering React,
 * so a pointer crossing the panel costs one style write per frame and no
 * reconciliation. The same effect with `useState` would re-render the panel,
 * and every card in it, sixty times a second — for an animation nobody can
 * read text through while it runs.
 *
 * **Touch opts out.** `pointerType !== 'mouse'` skips everything, because a
 * finger dragging across the panel while scrolling would smear the cards about,
 * and unlike a mouse there is no hover-out to put them back.
 *
 * **Reduced motion opts out too, and this is one of the few places that has to
 * be checked by hand.** `MotionConfig reducedMotion="user"` in `App.tsx`
 * disables declarations that motion itself animates — the entry fades, the
 * keyframe loops — but a motion value's transform is written directly by this
 * code, so nothing upstream is in a position to intercept it.
 *
 * The springs are soft and shared: deriving a larger offset with `useTransform`
 * on the returned value rather than making a second spring keeps the layers
 * moving together, so they read as one object at two depths rather than two
 * objects that happen to be near each other.
 */
export function usePointerParallax() {
  const reducedMotion = useReducedMotion()

  const x = useMotionValue(0)
  const y = useMotionValue(0)

  const springX = useSpring(x, { stiffness: 120, damping: 22, mass: 0.4 })
  const springY = useSpring(y, { stiffness: 120, damping: 22, mass: 0.4 })

  function onPointerMove(event: PointerEvent<HTMLElement>) {
    if (reducedMotion || event.pointerType !== 'mouse') {
      return
    }

    const bounds = event.currentTarget.getBoundingClientRect()

    // Guarded because a `display: none` ancestor (the panel below `lg`, where
    // this element has no box) reports a zero-sized rect, and dividing by it
    // would write `Infinity` into the transform.
    if (bounds.width === 0 || bounds.height === 0) {
      return
    }

    x.set(((event.clientX - bounds.left) / bounds.width - 0.5) * TRAVEL_X)
    y.set(((event.clientY - bounds.top) / bounds.height - 0.5) * TRAVEL_Y)
  }

  function onPointerLeave() {
    // Eased back by the spring rather than snapped, so leaving the panel looks
    // like letting go of something weighted.
    x.set(0)
    y.set(0)
  }

  return { onPointerMove, onPointerLeave, x: springX, y: springY }
}
