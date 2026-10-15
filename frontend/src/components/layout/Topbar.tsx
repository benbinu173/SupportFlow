import { Bell, CaretDown, Check, List, MagnifyingGlass, SignOut, UserCircle, X } from '@phosphor-icons/react'
import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { Avatar } from '@/components/ui/Avatar'
import { CountBadge } from '@/components/ui/Badge'
import { DropdownMenu, type DropdownMenuItem } from '@/components/ui/DropdownMenu'
import { IconButton } from '@/components/ui/IconButton'
import { Input } from '@/components/ui/Input'
import { Tooltip } from '@/components/ui/Tooltip'
import { can, isPortalRole, PERMISSIONS } from '@/features/auth/permissions'
import { useThemeStore, type ThemePreference } from '@/store/theme'
import type { SessionUser } from '@/types/session'
import { cn } from '@/utils/cn'

const THEME_CHOICES: Array<{ value: ThemePreference; label: string }> = [
  { value: 'light', label: 'Light theme' },
  { value: 'dark', label: 'Dark theme' },
  { value: 'system', label: 'System theme' },
]

export interface TopbarProps {
  user: SessionUser
  navOpen: boolean
  onToggleNav: () => void
  onSignOut: () => void
  unreadCount?: number
}

/**
 * The sticky header: navigation toggle, search, notifications, account.
 *
 * `backdrop-blur` is used here and nowhere else in the product. ADR-010 permits
 * it on fixed and sticky elements specifically because they are composited once;
 * the same filter on a scrolling list forces a repaint every frame.
 */
export function Topbar({
  user,
  navOpen,
  onToggleNav,
  onSignOut,
  unreadCount = 0,
}: TopbarProps) {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const themePreference = useThemeStore((state) => state.preference)
  const setThemePreference = useThemeStore((state) => state.setPreference)

  const isPortal = isPortalRole(user.role)
  const ticketsPath = isPortal ? '/portal' : '/tickets'

  const onSearch = (event: FormEvent) => {
    event.preventDefault()

    const trimmed = query.trim()

    if (!trimmed) {
      return
    }

    navigate(`${ticketsPath}?q=${encodeURIComponent(trimmed)}`)
    setQuery('')
  }

  // Mutually exclusive options, and the active one is marked twice: a check for
  // the eye, and the word "Active" for the ear. `meta` is inside the item's own
  // button, so the state is part of what a screen reader announces — the menu
  // pattern's `menuitemradio` would say it natively, but growing the menu API
  // for one caller is not worth it until a second one exists.
  const themeItems: DropdownMenuItem[] = THEME_CHOICES.map((choice, index) => {
    const selected = themePreference === choice.value

    return {
      id: `theme-${choice.value}`,
      label: choice.label,
      icon: selected ? <Check weight="bold" /> : <span className="block size-4" />,
      separatorBefore: index === 0,
      ...(selected ? { meta: 'Active' } : {}),
      onSelect: () => {
        setThemePreference(choice.value)
      },
    }
  })

  const menuItems: DropdownMenuItem[] = [
    {
      id: 'profile',
      label: 'Your profile',
      icon: <UserCircle />,
      onSelect: () => {
        navigate('/profile')
      },
    },
    ...themeItems,
    {
      id: 'sign-out',
      label: 'Sign out',
      icon: <SignOut />,
      danger: true,
      separatorBefore: true,
      onSelect: onSignOut,
    },
  ]

  return (
    <header
      className={cn(
        'sticky top-0 z-sticky flex h-14 items-center gap-2 border-b border-border-subtle',
        'bg-canvas/80 px-3 backdrop-blur-xl lg:px-6',
      )}
    >
      <IconButton
        aria-label={navOpen ? 'Close navigation' : 'Open navigation'}
        icon={navOpen ? <X /> : <List />}
        onClick={onToggleNav}
        aria-expanded={navOpen}
        className="lg:hidden"
      />

      {/* Staff only. A customer has a handful of tickets and a search box that
          reaches into a queue they cannot see would be a worse than useless
          affordance. */}
      {!isPortal && (
        <form onSubmit={onSearch} className="hidden min-w-0 flex-1 md:block" role="search">
          <Input
            type="search"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value)
            }}
            placeholder="Search tickets…"
            aria-label="Search tickets"
            leadingIcon={<MagnifyingGlass />}
            className="h-8 max-w-md"
          />
        </form>
      )}

      <div className="ml-auto flex items-center gap-1">
        {(can(user.role, PERMISSIONS.NOTIFICATION_LIST) || isPortal) && (
          <Tooltip content="Notifications">
            <IconButton
              aria-label={
                unreadCount > 0 ? `Notifications, ${unreadCount} unread` : 'Notifications'
              }
              icon={
                <>
                  <Bell />
                  {unreadCount > 0 && (
                    <span className="absolute -top-0.5 -right-0.5">
                      <CountBadge count={unreadCount} variant="danger" />
                    </span>
                  )}
                </>
              }
              onClick={() => {
                navigate('/notifications')
              }}
              className="relative"
            />
          </Tooltip>
        )}

        <DropdownMenu
          label="Account menu"
          align="end"
          items={menuItems}
          triggerClassName={cn(
            'ml-1 flex items-center gap-2 rounded-lg py-1 pr-2 pl-1',
            'transition-colors duration-150 ease-out-expo',
            'hover:bg-surface-hover',
          )}
          trigger={
            <>
              <Avatar name={user.name} size="sm" />
              <span className="hidden max-w-32 truncate text-sm font-medium text-ink sm:block">
                {user.name}
              </span>
              <CaretDown aria-hidden="true" className="size-3.5 text-ink-subtle" />
            </>
          }
        />
      </div>
    </header>
  )
}
