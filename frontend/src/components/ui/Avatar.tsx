import type { ComponentPropsWithRef } from 'react'

import { cn } from '@/utils/cn'

export type AvatarSize = 'xs' | 'sm' | 'md' | 'lg'

const SIZE_CLASSES: Record<AvatarSize, string> = {
  xs: 'size-5 text-[0.5625rem]',
  sm: 'size-6 text-[0.625rem]',
  md: 'size-8 text-xs',
  lg: 'size-10 text-sm',
}

/* Six tints drawn from tokens the rest of the product already uses, so an
   avatar cannot introduce a colour that appears nowhere else and so all six
   resolve correctly in both themes. An arbitrary generated hue would have to
   be re-tuned for dark mode and would drift from the palette in the meantime. */
const TINTS = [
  'bg-brand-subtle text-brand-text',
  'bg-success-subtle text-success-text',
  'bg-info-subtle text-info-text',
  'bg-warning-subtle text-warning-text',
  'bg-ai-subtle text-ai',
  'bg-surface-sunken text-ink-muted',
] as const

/** Stable within a session and across them, so the same person is the same
 *  colour on every screen. */
function tintIndex(seed: string): number {
  let hash = 0

  for (let i = 0; i < seed.length; i += 1) {
    hash = (hash * 31 + seed.charCodeAt(i)) | 0
  }

  return Math.abs(hash) % TINTS.length
}

/** Not exported: nothing outside this file has needed the initials on their
 *  own, and a component module that also exports a helper stops being
 *  fast-refreshable for no gain. Export it here the day a caller appears. */
function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)

  if (parts.length === 0) {
    return '?'
  }

  const first = parts[0]?.[0] ?? ''
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? '') : ''

  return (first + last).toUpperCase() || '?'
}

export type AvatarProps = Omit<ComponentPropsWithRef<'span'>, 'children'> & {
  name: string
  size?: AvatarSize
}

/**
 * Initials, coloured by a hash of the name.
 *
 * Deliberately not an image component. A support desk has no avatars for its
 * customers, and a web of fallback images that usually 404 is worse than a
 * consistent monogram. When agent photos do exist, an `<img>` slots in here
 * without any call site changing.
 */
export function Avatar({ name, size = 'md', className, ...rest }: AvatarProps) {
  const initials = initialsOf(name)

  return (
    <span
      // The initials are a visual abbreviation of a name that is usually
      // already on screen next to them; announcing "JD" as well is noise.
      aria-hidden="true"
      title={name}
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-full font-semibold',
        'select-none uppercase',
        SIZE_CLASSES[size],
        TINTS[tintIndex(name)],
        className,
      )}
      {...rest}
    >
      {initials}
    </span>
  )
}
