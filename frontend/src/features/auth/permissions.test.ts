import { describe, expect, it } from 'vitest'

import {
  PERMISSIONS,
  ROW_SCOPES,
  ROLE_PERMISSIONS,
  TICKET_SCOPE_BY_ROLE,
  USER_ROLES,
  can,
  canAny,
  isPortalRole,
  type Permission,
} from './permissions'

const ALL: Permission[] = Object.values(PERMISSIONS)

describe('permissions — the mirror of the Python table', () => {
  it('grants admin every capability', () => {
    // Admin is written out as `ALL_PERMISSIONS` rather than derived, so this
    // asserts the derivation still covers the whole object.
    expect(ROLE_PERMISSIONS.admin.size).toBe(ALL.length)

    for (const permission of ALL) {
      expect(can('admin', permission)).toBe(true)
    }
  })

  it('denies a capability to the roles that do not hold it', () => {
    // The specific absences the backend's matrix encodes. Each one is a
    // different screen: an org setting nobody can reach, a team page the
    // manager can read but not edit, an Assign button an agent must not see.
    expect(can('manager', PERMISSIONS.ORG_UPDATE)).toBe(false)
    expect(can('manager', PERMISSIONS.USER_CREATE)).toBe(false)
    expect(can('manager', PERMISSIONS.AI_CONFIGURE)).toBe(false)
    expect(can('agent', PERMISSIONS.TICKET_ASSIGN)).toBe(false)
    expect(can('agent', PERMISSIONS.TICKET_CHANGE_PRIORITY)).toBe(false)
    expect(can('agent', PERMISSIONS.USER_LIST)).toBe(false)
    expect(can('agent', PERMISSIONS.ANALYTICS_ORG)).toBe(false)
  })

  it('keeps the customer role out of internal notes, AI, and the knowledge base', () => {
    // §32 and §41 in one assertion. A leak here renders a note the customer was
    // never meant to read, which is the failure mode the portal phase is built
    // to catch — this is the cheaper half of that check.
    for (const permission of ALL) {
      if (permission.startsWith('ai:')) {
        expect(can('customer', permission)).toBe(false)
      }
    }

    expect(can('customer', PERMISSIONS.MESSAGE_READ_INTERNAL)).toBe(false)
    expect(can('customer', PERMISSIONS.MESSAGE_POST_INTERNAL)).toBe(false)
    expect(can('customer', PERMISSIONS.KB_LIST)).toBe(false)
    expect(can('customer', PERMISSIONS.CUSTOMER_LIST)).toBe(false)
    expect(can('customer', PERMISSIONS.SLA_VIEW)).toBe(false)
  })

  it('lets every role see and reply to its own tickets', () => {
    for (const role of USER_ROLES) {
      expect(can(role, PERMISSIONS.TICKET_LIST)).toBe(true)
      expect(can(role, PERMISSIONS.TICKET_VIEW)).toBe(true)
      expect(can(role, PERMISSIONS.MESSAGE_POST_REPLY)).toBe(true)
      expect(can(role, PERMISSIONS.PROFILE_VIEW)).toBe(true)
    }
  })

  it('denies rather than throws for a role that is absent', () => {
    // The safe failure is a hidden button, never a shown one — and never a
    // crash on the way to rendering a screen.
    expect(can(null, PERMISSIONS.TICKET_VIEW)).toBe(false)
    expect(can(undefined, PERMISSIONS.TICKET_VIEW)).toBe(false)
    expect(can('owner' as never, PERMISSIONS.TICKET_VIEW)).toBe(false)
  })

  it('answers canAny for a disjunction', () => {
    expect(canAny('agent', [PERMISSIONS.ANALYTICS_ORG, PERMISSIONS.ANALYTICS_OWN])).toBe(true)
    expect(canAny('customer', [PERMISSIONS.ANALYTICS_ORG, PERMISSIONS.ANALYTICS_OWN])).toBe(false)
    expect(canAny('agent', [])).toBe(false)
  })
})

describe('row scopes', () => {
  it('narrows from organization to own as the role narrows', () => {
    expect(TICKET_SCOPE_BY_ROLE.admin).toBe('organization')
    expect(TICKET_SCOPE_BY_ROLE.manager).toBe('organization')
    expect(TICKET_SCOPE_BY_ROLE.agent).toBe('assigned')
    expect(TICKET_SCOPE_BY_ROLE.customer).toBe('own')
  })

  it('uses only declared scopes', () => {
    for (const scope of Object.values(TICKET_SCOPE_BY_ROLE)) {
      expect(ROW_SCOPES).toContain(scope)
    }
  })
})

describe('portal roles', () => {
  it('treats only the customer as an external party', () => {
    expect(isPortalRole('customer')).toBe(true)
    expect(isPortalRole('agent')).toBe(false)
    expect(isPortalRole('manager')).toBe(false)
    expect(isPortalRole('admin')).toBe(false)
  })
})
