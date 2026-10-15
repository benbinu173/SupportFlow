import { cn } from '@/utils/cn'

/**
 * The product mark.
 *
 * Two superimposed rounded squares — a smaller pane sitting in a larger tray,
 * which is the double-bezel idea from ADR-010 reduced to a glyph. Drawn rather
 * than imported so it inherits `currentColor` and needs no asset pipeline, and
 * so the geometry is one edit away if the mark ever changes.
 */
export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn('flex items-center gap-2.5', className)}>
      <svg
        viewBox="0 0 32 32"
        fill="none"
        aria-hidden="true"
        className="size-6 shrink-0 text-brand"
      >
        <rect
          x="1"
          y="1"
          width="30"
          height="30"
          rx="9"
          fill="currentColor"
          fillOpacity="0.12"
          stroke="currentColor"
          strokeOpacity="0.25"
          strokeWidth="1"
        />
        <rect x="8" y="8" width="16" height="16" rx="5.5" fill="currentColor" />
      </svg>

      <span className="text-[0.9375rem] font-semibold tracking-[-0.02em] text-ink">
        SupportFlow
      </span>
    </span>
  )
}
