import { NavLink } from 'react-router-dom'

import type { NavSection } from '@/routes/navigation'
import { cn } from '@/utils/cn'

export interface SidebarNavProps {
  sections: NavSection[]
  /** Called when a link is followed — closes the mobile panel, so following a
   *  link does not leave it open on top of the page it navigated to. */
  onNavigate?: () => void
}

/**
 * The navigation list, rendered identically in the desktop sidebar and the
 * mobile panel.
 *
 * The active item carries both a tinted background and a small brand bar at its
 * left edge. The tint alone is easy to lose among a column of hover states; the
 * bar gives the current location a shape as well as a colour, which is what
 * makes it findable at a glance and not just on comparison.
 */
export function SidebarNav({ sections, onNavigate }: SidebarNavProps) {
  return (
    <nav aria-label="Main" className="flex flex-col gap-5">
      {sections.map((section) => (
        <div key={section.id} className="flex flex-col gap-0.5">
          {section.label && (
            <p className="eyebrow px-2.5 pb-1.5 text-ink-subtle">{section.label}</p>
          )}

          {section.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              {...(item.end ? { end: true } : {})}
              {...(onNavigate ? { onClick: onNavigate } : {})}
              className={({ isActive }) =>
                cn(
                  'relative flex items-center gap-2.5 rounded-lg px-2.5 py-1.5',
                  'text-sm font-medium',
                  'transition-colors duration-150 ease-out-expo',
                  '[&_svg]:size-4',
                  isActive
                    ? 'bg-brand-subtle text-brand-text'
                    : 'text-ink-muted hover:bg-surface-hover hover:text-ink',
                )
              }
            >
              {({ isActive }) => (
                <>
                  {isActive && (
                    <span
                      aria-hidden="true"
                      className="absolute top-1/2 left-0 h-4 w-0.5 -translate-y-1/2 rounded-full bg-brand"
                    />
                  )}
                  {item.icon}
                  <span className="truncate">{item.label}</span>
                </>
              )}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  )
}
