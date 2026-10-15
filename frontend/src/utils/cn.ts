import { twMerge } from 'tailwind-merge'

export type ClassValue = string | false | null | undefined

/**
 * Conditional class names, with later Tailwind utilities winning conflicts.
 *
 * The second half is why this exists rather than a `filter(Boolean).join(' ')`.
 * A Button that hardcodes `px-4` and a caller that passes `className="px-2"`
 * both reach the class attribute, and which one applies is decided by CSS
 * source order — Tailwind's, not the caller's. That makes a component's
 * `className` prop unreliable in exactly the case people reach for it.
 * `twMerge` resolves the conflict so the caller wins.
 */
export function cn(...inputs: ClassValue[]): string {
  const classes: string[] = []

  for (const input of inputs) {
    if (input) {
      classes.push(input)
    }
  }

  return twMerge(classes.join(' '))
}
