/**
 * The shared appearance of every text-entry control.
 *
 * One declaration rather than three near-identical ones, because an input, a
 * textarea and a select that differ in border weight or focus ring are the
 * kind of inconsistency that is invisible in isolation and obvious in a form.
 * They differ only in shape and padding.
 *
 * On the border weight: `--color-border-strong` is the token defined to clear
 * 3:1 against the surface, which is what WCAG 1.4.11 asks of the boundary of a
 * control. The lighter `--color-border` is for decorative edges and would leave
 * an input identifiable only by its fill, which is a considerably weaker
 * signal than it looks.
 */
export const CONTROL_CLASSES = [
  'w-full border border-border-strong bg-surface text-ink',
  'placeholder:text-ink-subtle',
  'transition-[color,border-color,box-shadow,background-color] duration-150 ease-out-expo',
  'hover:border-ink-subtle',
  // `:focus`, not `:focus-visible` — a pointer user who clicks into a text
  // field has also put the keyboard there, and the caret alone is a weak cue.
  'focus:border-brand focus:outline-none focus:ring-4 focus:ring-brand/15',
  'aria-invalid:border-danger aria-invalid:hover:border-danger',
  'aria-invalid:focus:border-danger aria-invalid:focus:ring-danger/15',
  'disabled:cursor-not-allowed disabled:border-border disabled:bg-surface-sunken disabled:text-ink-subtle',
].join(' ')

/** Reserved space for a leading icon, so text does not run underneath it. */
export const CONTROL_WITH_LEADING_ICON_CLASSES = 'pl-9'

/** Reserved space for a trailing affordance (a select's chevron, a clear button). */
export const CONTROL_WITH_TRAILING_AFFORDANCE_CLASSES = 'pr-9'
