import { Check, Clock, Monitor, Moon, ShieldCheck, Sun, UserCircle } from '@phosphor-icons/react'
import { useQuery } from '@tanstack/react-query'

import { Badge } from '@/components/ui/Badge'
import { Card, PageHeader } from '@/components/ui/Card'
import { ErrorState } from '@/components/ui/States'
import { Skeleton } from '@/components/ui/Skeleton'
import { profileKeys } from '@/features/profile/keys'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { useSessionStore } from '@/store/auth'
import { useThemeStore, type ThemePreference } from '@/store/theme'
import type { components } from '@/types/api'

type Profile = components['schemas']['UserRead']

const ROLE_LABELS: Record<Profile['role'], string> = {
  admin: 'Administrator',
  manager: 'Manager',
  agent: 'Support agent',
  customer: 'Customer',
}

const THEMES: { value: ThemePreference; label: string; detail: string; icon: typeof Sun }[] = [
  { value: 'system', label: 'System', detail: 'Follow your device appearance', icon: Monitor },
  { value: 'light', label: 'Light', detail: 'A bright, clear workspace', icon: Sun },
  { value: 'dark', label: 'Dark', detail: 'A low-light workspace', icon: Moon },
]

function accountDate(value: string | null): string {
  return value ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Not recorded'
}

export function PreferencesPage() {
  const sessionUser = useSessionStore((state) => state.user)
  const preference = useThemeStore((state) => state.preference)
  const setPreference = useThemeStore((state) => state.setPreference)
  const profile = useQuery({
    queryKey: profileKeys.me(),
    queryFn: async () => (await apiClient.get<Profile>('/auth/me')).data,
    enabled: Boolean(sessionUser),
    staleTime: 60_000,
  })

  if (!sessionUser) return null
  const account = profile.data ?? sessionUser
  const initials = account.name.trim().split(/\s+/).slice(0, 2).map((part) => part[0]?.toUpperCase() ?? '').join('')

  return (
    <AppShell user={sessionUser} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-5xl flex-col gap-6">
        <PageHeader title="Account preferences" description="Manage how SupportFlow looks and review your account details." />

        {profile.error && <ErrorState title="Couldn’t refresh your profile" error={profile.error} onRetry={() => { void profile.refetch() }} />}

        <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1.15fr)_minmax(18rem,0.85fr)]">
          <Card className="overflow-hidden">
            <div className="border-b border-border-subtle px-5 py-4"><h2 className="text-sm font-semibold text-ink">Your account</h2><p className="mt-1 text-xs text-ink-muted">Identity and access details from your active profile.</p></div>
            {profile.isPending ? <div className="space-y-4 p-5"><Skeleton className="h-16 rounded-xl" /><Skeleton className="h-10 rounded-lg" /><Skeleton className="h-10 rounded-lg" /><Skeleton className="h-10 rounded-lg" /></div> : <div className="p-5">
              <div className="flex items-center gap-3 rounded-xl bg-surface-sunken/60 p-4"><span className="grid size-12 shrink-0 place-items-center rounded-2xl bg-brand-subtle text-sm font-semibold text-brand-text">{initials || <UserCircle aria-hidden="true" className="size-6" />}</span><div className="min-w-0 flex-1"><p className="truncate text-sm font-semibold text-ink">{account.name}</p><p className="mt-0.5 truncate text-xs text-ink-muted">{account.email}</p></div><Badge variant={account.is_active ? 'success' : 'neutral'} dot>{account.is_active ? 'Active' : 'Inactive'}</Badge></div>

              <dl className="mt-5 divide-y divide-border-subtle">
                <div className="flex items-center justify-between gap-4 py-3"><dt className="text-xs text-ink-muted">Role</dt><dd className="text-xs font-medium text-ink">{ROLE_LABELS[account.role]}</dd></div>
                <div className="flex items-center justify-between gap-4 py-3"><dt className="text-xs text-ink-muted">Account created</dt><dd className="text-right text-xs tabular-nums text-ink">{accountDate(account.created_at)}</dd></div>
                <div className="flex items-center justify-between gap-4 py-3"><dt className="text-xs text-ink-muted">Last sign in</dt><dd className="text-right text-xs tabular-nums text-ink">{accountDate(account.last_login_at)}</dd></div>
              </dl>
            </div>}
            <div className="flex items-start gap-2 border-t border-border-subtle bg-surface-sunken/40 px-5 py-3.5 text-xs leading-relaxed text-ink-muted"><ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-success-text" /><p>Your access level is managed by your organization. Contact an administrator if your role needs to change.</p></div>
          </Card>

          <Card className="overflow-hidden">
            <div className="border-b border-border-subtle px-5 py-4"><h2 className="text-sm font-semibold text-ink">Appearance</h2><p className="mt-1 text-xs text-ink-muted">Choose a theme for this browser.</p></div>
            <div className="space-y-2 p-4">
              {THEMES.map(({ value, label, detail, icon: Icon }) => {
                const selected = preference === value
                return <button key={value} type="button" aria-pressed={selected} onClick={() => setPreference(value)} className={`flex w-full items-center gap-3 rounded-xl border p-3 text-left transition-colors ${selected ? 'border-brand-border bg-brand-subtle/50' : 'border-border-subtle bg-surface hover:bg-surface-sunken'}`}>
                  <span className={`grid size-9 shrink-0 place-items-center rounded-lg ${selected ? 'bg-brand-subtle text-brand-text' : 'bg-surface-sunken text-ink-muted'}`}><Icon aria-hidden="true" className="size-4" /></span>
                  <span className="min-w-0 flex-1"><span className="block text-xs font-medium text-ink">{label}</span><span className="mt-0.5 block text-[0.6875rem] text-ink-subtle">{detail}</span></span>
                  {selected && <Check aria-hidden="true" className="size-4 shrink-0 text-brand-text" />}
                </button>
              })}
            </div>
            <div className="flex items-center gap-2 border-t border-border-subtle px-5 py-3.5 text-[0.6875rem] text-ink-subtle"><Clock aria-hidden="true" className="size-3.5" /><span>Theme choice is saved on this device and applies immediately.</span></div>
          </Card>
        </div>
      </div>
    </AppShell>
  )
}
