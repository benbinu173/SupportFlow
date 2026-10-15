import type { ComponentPropsWithRef } from 'react'

import { cn } from '@/utils/cn'

export type SkeletonProps = ComponentPropsWithRef<'div'>

/**
 * A placeholder that stands in for content while it loads.
 *
 * The sweep is a `::after` pseudo-element rather than a child, so a Skeleton
 * can be dropped anywhere without altering the layout it is standing in for.
 * The animation is a translate — see the note on `--animate-shimmer` in
 * `index.css` for why it is not a background-position animation.
 *
 * Always `aria-hidden`: a screen reader gets the loading state from the
 * container's `aria-busy`, not from twenty unlabelled boxes.
 */
export function Skeleton({ className, ...rest }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={cn(
        'relative overflow-hidden rounded-md bg-surface-sunken',
        'after:absolute after:inset-0 after:animate-shimmer',
        'after:bg-linear-to-r after:from-transparent after:to-transparent',
        'after:via-white/30 dark:after:via-white/8',
        className,
      )}
      {...rest}
    />
  )
}
