import { useCallback } from 'react'

import { Badge } from '@/components/ui/Badge'
import { Card, PageHeader } from '@/components/ui/Card'
import { AppShell } from '@/layouts/AppShell'
import { can, PERMISSIONS, TICKET_SCOPE_BY_ROLE } from '@/features/auth/permissions'
import { logout } from '@/services/api/auth'
import { useSessionStore } from '@/store/auth'

/**
 * Where every role lands after signing in.
 *
 * **This is a placeholder, and it says so.** The ticket queue arrives in F4 and
 * the dashboards in F7; what F2 is responsible for is proving the session works
 * end to end — that a real login returns a real token, that `/auth/me` reports
 * the real role, and that the shell renders the navigation that role is
 * entitled to. A page claiming to be a dashboard while showing nothing would be
 * exactly the fabrication §60 rules out, so this one states its purpose instead.
 *
 * It is replaced screen by screen: `/dashboard` by F7, `/portal` by F9.
 */
export function HomePage() {
  const user = useSessionStore((state) => state.user)

  // `logout` ends the session locally in a `finally`, so the `RequireAuth` guard
  // above this page reacts to the status change and redirects on its own. There
  // is no navigate() here on purpose: two mechanisms racing to move the user
  // would occasionally fight, and one of them is enough.
  const handleSignOut = useCallback(() => {
    void logout()
  }, [])

  if (!user) {
    return null
  }

  const scope = TICKET_SCOPE_BY_ROLE[user.role]

  const scopeCopy: Record<typeof scope, string> = {
    organization: 'every ticket in the organization',
    assigned: 'the tickets assigned to you',
    own: 'your own requests',
  }

  return (
    <AppShell user={user} onSignOut={handleSignOut}>
      <div className="flex flex-col gap-6">
        <PageHeader
          title={`Signed in as ${user.name}`}
          description={`Your role reaches ${scopeCopy[scope]}.`}
          actions={<Badge variant="brand">{user.role}</Badge>}
        />

        <Card variant="bezel">
          <div className="flex flex-col gap-4 px-5 py-5">
            <div className="flex flex-col gap-1">
              <h2 className="text-sm font-semibold text-ink">What this page is</h2>
              <p className="text-[0.8125rem] leading-relaxed text-ink-muted">
                A checkpoint for the sign-in flow. The navigation beside it is real — it is
                filtered by your role&apos;s permissions, so a customer cannot see the admin
                section and an agent is not offered the audit log. The screens those links point
                at arrive over the next phases.
              </p>
            </div>

            <dl className="grid gap-x-6 gap-y-3 border-t border-border-subtle pt-4 sm:grid-cols-2">
              <div className="flex flex-col gap-0.5">
                <dt className="text-2xs tracking-wide text-ink-subtle uppercase">Email</dt>
                <dd className="text-[0.8125rem] text-ink">{user.email}</dd>
              </div>

              <div className="flex flex-col gap-0.5">
                <dt className="text-2xs tracking-wide text-ink-subtle uppercase">Account</dt>
                <dd className="text-[0.8125rem] text-ink">
                  {user.is_active ? 'Active' : 'Deactivated'}
                </dd>
              </div>

              {/* The permission check is shown rather than merely applied: it is
                  the one thing here that can be verified against the server by
                  logging in twice with different roles. */}
              <div className="flex flex-col gap-0.5">
                <dt className="text-2xs tracking-wide text-ink-subtle uppercase">
                  Can assign tickets
                </dt>
                <dd className="text-[0.8125rem] text-ink">
                  {can(user.role, PERMISSIONS.TICKET_ASSIGN) ? 'Yes' : 'No'}
                </dd>
              </div>

              <div className="flex flex-col gap-0.5">
                <dt className="text-2xs tracking-wide text-ink-subtle uppercase">
                  Can read internal notes
                </dt>
                <dd className="text-[0.8125rem] text-ink">
                  {can(user.role, PERMISSIONS.MESSAGE_READ_INTERNAL) ? 'Yes' : 'No'}
                </dd>
              </div>
            </dl>
          </div>
        </Card>
      </div>
    </AppShell>
  )
}
