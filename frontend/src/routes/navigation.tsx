import {
  BookOpen,
  ChartLine,
  GearSix,
  Plus,
  Scroll,
  SquaresFour,
  Ticket,
  Timer,
  UserGear,
  Users,
} from '@phosphor-icons/react'
import type { ReactNode } from 'react'

import { can, PERMISSIONS, type Permission, type UserRole } from '@/features/auth/permissions'

export interface NavItem {
  to: string
  label: string
  icon: ReactNode
  /** Omitted for a destination every signed-in role may open. */
  permission?: Permission
  /** Match the path exactly, so a parent route is not marked active while a
   *  child is open. */
  end?: boolean
}

export interface NavSection {
  id: string
  label?: string
  items: NavItem[]
}

/* An icon carries no meaning to a screen reader — the label beside it is the
   accessible name — so it is hidden rather than relied upon. Stroke weight is
   set once globally by IconProvider; ADR-010 bans the thick default. */
function icon(node: ReactNode): ReactNode {
  return (
    <span aria-hidden="true" className="flex shrink-0">
      {node}
    </span>
  )
}

const STAFF_SECTIONS: NavSection[] = [
  {
    id: 'work',
    items: [
      { to: '/dashboard', label: 'Dashboard', icon: icon(<SquaresFour />) },
      { to: '/tickets', label: 'Tickets', icon: icon(<Ticket />) },
      { to: '/customers', label: 'Customers', icon: icon(<Users />) },
    ],
  },
  {
    id: 'insight',
    label: 'Insight',
    items: [
      {
        to: '/analytics',
        label: 'Analytics',
        icon: icon(<ChartLine />),
        permission: PERMISSIONS.ANALYTICS_ORG,
      },
      { to: '/sla', label: 'SLA', icon: icon(<Timer />), permission: PERMISSIONS.SLA_VIEW },
    ],
  },
  {
    id: 'knowledge',
    label: 'Knowledge',
    items: [
      {
        to: '/knowledge',
        label: 'Knowledge base',
        icon: icon(<BookOpen />),
        permission: PERMISSIONS.KB_LIST,
      },
    ],
  },
  {
    id: 'admin',
    label: 'Administration',
    items: [
      { to: '/users', label: 'Team', icon: icon(<UserGear />), permission: PERMISSIONS.USER_LIST },
      {
        to: '/audit',
        label: 'Audit log',
        icon: icon(<Scroll />),
        permission: PERMISSIONS.AUDIT_VIEW,
      },
      {
        to: '/settings',
        label: 'Settings',
        icon: icon(<GearSix />),
        permission: PERMISSIONS.ORG_VIEW,
      },
    ],
  },
]

const PORTAL_SECTIONS: NavSection[] = [
  {
    id: 'portal',
    items: [
      { to: '/portal', label: 'My requests', icon: icon(<Ticket />), end: true },
      { to: '/portal/new', label: 'New request', icon: icon(<Plus />) },
    ],
  },
]

/**
 * The navigation a role actually sees.
 *
 * Filtering happens here rather than inside the sidebar, so what a role can
 * reach is one readable list instead of a conditional scattered through markup.
 * A section left with no items is dropped entirely — the alternative is a
 * heading like "Administration" with nothing under it, which tells a user the
 * product has a feature they are not allowed to have.
 *
 * As everywhere else, this is a usability affordance. The routes are enforced
 * server-side; hiding a link is a courtesy, not a control.
 */
export function navigationFor(role: UserRole): NavSection[] {
  const sections = role === 'customer' ? PORTAL_SECTIONS : STAFF_SECTIONS

  return sections
    .map((section) => ({
      ...section,
      items: section.items.filter(
        (item) => item.permission === undefined || can(role, item.permission),
      ),
    }))
    .filter((section) => section.items.length > 0)
}

/** Where a role lands after signing in. */
export const HOME_BY_ROLE: Record<UserRole, string> = {
  admin: '/dashboard',
  manager: '/dashboard',
  agent: '/dashboard',
  customer: '/portal',
}
