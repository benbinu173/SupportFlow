import { motion, useTransform, type MotionValue } from 'motion/react'
import type { CSSProperties } from 'react'

/**
 * What the panel shows while the form is being filled in.
 *
 * Every string here describes something the product does, because the panel's
 * whole job is to say what the account is for. A sign-in screen advertising a
 * feature the app does not have is a worse first impression than a plain one,
 * so the cards carry the ticket lifecycle, the confidentiality rule on internal
 * notes, and the role matrix — all of which exist in the server today.
 */
export type PanelCard =
  | { kind: 'chips'; eyebrow: string; chips: readonly string[] }
  | { kind: 'prose'; eyebrow: string; body: string }

/**
 * The light behind the panel.
 *
 * Two pools of brand colour rather than one, offset and out of phase, because a
 * single soft light reads as a gradient and two read as a room. `animation-
 * delay` staggers them so the movement never resolves into a pulse.
 *
 * Radial gradients, not `backdrop-filter` or a CSS `filter: blur()`. The blur
 * version repaints a full-panel surface every frame for the whole life of the
 * page; a gradient is rasterised once and thereafter only transformed. That
 * trade is why this app confines backdrop blur to fixed and sticky elements,
 * and a decorative orb has no business costing more than a tooltip.
 *
 * Monochrome on purpose. The panel sits on `--color-surface-inverse`, which
 * *flips* between themes — dark navy in light mode, near-white in dark mode —
 * and the status and priority tokens flip with it. A chip coloured "green means
 * done" would be a different colour on the other background, so the depth here
 * is carried by light rather than by hue.
 */
export function AuthPanelGlow() {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      <div className="animate-drift absolute -top-40 -left-32 size-[38rem] rounded-full opacity-40 [background:radial-gradient(circle_at_center,var(--color-brand)_0%,transparent_62%)]" />
      <div className="animate-drift absolute -right-28 -bottom-24 size-[30rem] rounded-full opacity-30 [animation-delay:-9s] [background:radial-gradient(circle_at_center,var(--color-brand)_0%,transparent_60%)]" />
    </div>
  )
}

/** One card's face, on the inverse surface. */
function PanelCardFace({ card }: { card: PanelCard }) {
  return (
    <div className="rounded-2xl border border-ink-inverse/10 bg-ink-inverse/[0.05] p-4 shadow-xl shadow-black/20">
      <p className="eyebrow text-ink-inverse/45">{card.eyebrow}</p>

      {card.kind === 'chips' ? (
        <ul className="mt-3 flex flex-wrap gap-1.5">
          {card.chips.map((chip) => (
            <li
              key={chip}
              className="rounded-full border border-ink-inverse/12 bg-ink-inverse/[0.07] px-2.5 py-1 text-[0.6875rem] font-medium text-ink-inverse/80"
            >
              {chip}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2.5 text-[0.8125rem] leading-relaxed text-ink-inverse/75">{card.body}</p>
      )}
    </div>
  )
}

/**
 * The cards, stacked at two depths and tilted off the grid.
 *
 * **Why the nesting.** The outer element carries the pointer parallax as motion
 * values on `style`, and the inner one carries the entrance as a CSS animation.
 * They are separate elements because both write `transform` — and motion
 * composes `x`, `y` and `rotate` into that single property, so a parallax
 * setting `x` on the element an entrance was already rotating would win
 * outright and hand back an element that had never been tilted. The inline
 * `transform` on the inner element is its *resting* rotation, and the keyframe
 * only supplies a `from` frame, so the animation lands on exactly it.
 *
 * **Why two transforms and not one.** A single offset applied to the whole
 * stack is just the stack sliding around. Scaled differently, the near card
 * moves further than the far one and the pair reads as having depth — which is
 * the entire reason the pointer handler exists.
 */
export function AuthPanelCards({
  cards,
  x,
  y,
}: {
  cards: readonly PanelCard[]
  x: MotionValue<number>
  y: MotionValue<number>
}) {
  const nearX = useTransform(x, (value) => value * 1.7)
  const nearY = useTransform(y, (value) => value * 1.7)

  return (
    <div className="relative flex w-full max-w-[22rem] flex-col">
      {cards.map((card, index) => {
        // Two cards, one behind the other. A third would take the front card's
        // treatment, which is a reasonable thing for it to become.
        const behind = index === 0
        const restingTilt = behind ? -4 : 1.5

        return (
          <motion.div
            key={card.eyebrow}
            style={{ x: behind ? x : nearX, y: behind ? y : nearY }}
            className={behind ? 'w-[88%]' : 'ml-auto -mt-8 w-[94%]'}
          >
            <div
              className="animate-enter"
              style={
                {
                  transform: `translate3d(0, 0, 0) rotate(${String(restingTilt)}deg)`,
                  '--enter-rotate': `${String(behind ? -11 : -3)}deg`,
                  '--enter-y': '36px',
                  '--enter-delay': `${String(300 + index * 130)}ms`,
                } as CSSProperties
              }
            >
              <PanelCardFace card={card} />
            </div>
          </motion.div>
        )
      })}
    </div>
  )
}
