import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'

import { Logo } from '@/components/layout/Logo'
import { SidebarNav } from '@/components/layout/SidebarNav'
import { Topbar } from '@/components/layout/Topbar'
import { navigationFor } from '@/routes/navigation'
import type { SessionUser } from '@/types/session'

export interface AppShellProps {
  user: SessionUser
  onSignOut: () => void
  unreadCount?: number
  children: ReactNode
}

/**
 * The frame every authenticated screen sits in.
 *
 * A fixed sidebar beside a column that scrolls independently, so the nav stays
 * put while a long ticket thread moves. Below `lg` the sidebar is replaced by a
 * panel that opens under the header — deliberately not a modal drawer. A drawer
 * over an always-visible page is a modal, and a modal that exists only to show
 * six links is a focus trap and a scroll lock bought for nothing.
 */
export function AppShell({ user, onSignOut, unreadCount = 0, children }: AppShellProps) {
  const [navOpen, setNavOpen] = useState(false)
  const location = useLocation()
  const sections = navigationFor(user.role)

  const closeNav = useCallback(() => {
    setNavOpen(false)
  }, [])

  // Following a link should not leave the panel open over the page it opened.
  //
  // Written as a render-time reset rather than an effect on `location.pathname`.
  // An effect would commit the new route first and close the panel on the next
  // pass, so the panel is briefly painted over the page it just opened — and
  // that intermediate frame is a visible flicker, not a theoretical one.
  const [lastPath, setLastPath] = useState(location.pathname)

  if (lastPath !== location.pathname) {
    setLastPath(location.pathname)
    setNavOpen(false)
  }

  useEffect(() => {
    if (!navOpen) {
      return
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setNavOpen(false)
      }
    }

    document.addEventListener('keydown', onKeyDown)

    return () => {
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [navOpen])

  return (
    <div className="min-h-dvh bg-canvas">
      <aside className="fixed inset-y-0 left-0 z-sticky hidden w-60 flex-col border-r border-border-subtle bg-surface lg:flex">
        <div className="flex h-14 shrink-0 items-center px-4">
          <Logo />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2.5 pb-4">
          <SidebarNav sections={sections} />
        </div>
      </aside>

      <div className="lg:pl-60">
        <Topbar
          user={user}
          navOpen={navOpen}
          onToggleNav={() => {
            setNavOpen((value) => !value)
          }}
          onSignOut={onSignOut}
          unreadCount={unreadCount}
        />

        {navOpen && (
          <div className="border-b border-border-subtle bg-surface px-3 py-3 lg:hidden">
            <SidebarNav sections={sections} onNavigate={closeNav} />
          </div>
        )}

        {/* The max width is generous on purpose: a ticket table wants the room,
            and a reading column is a per-screen choice made with `max-w-*`
            rather than something imposed on every page at once. */}
        <main className="mx-auto w-full max-w-[110rem] px-4 py-6 lg:px-8">{children}</main>
      </div>
    </div>
  )
}
