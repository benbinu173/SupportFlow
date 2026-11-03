import { Check, Copy, UserPlus, UsersThree } from '@phosphor-icons/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'

import { Avatar } from '@/components/ui/Avatar'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/Dialog'
import { Dialog } from '@/components/ui/Dialog'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Field } from '@/components/ui/Field'
import { Input } from '@/components/ui/Input'
import { Select } from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { can, PERMISSIONS } from '@/features/auth/permissions'
import { customerKeys } from '@/features/customers/keys'
import { teamKeys } from '@/features/team/keys'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { toApiError } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import { toast } from '@/store/toasts'
import type { components } from '@/types/api'

type User = components['schemas']['UserRead']
type UserRole = components['schemas']['UserRole']
type Customer = components['schemas']['CustomerRead']
type CreateUserPayload = components['schemas']['UserCreate']

const PAGE_SIZE = 50
const USER_ROLES: UserRole[] = ['admin', 'manager', 'agent', 'customer']
const STAFF_ROLES: UserRole[] = ['admin', 'manager', 'agent']
const ROLE_LABELS: Record<UserRole, string> = {
  admin: 'Administrator',
  manager: 'Manager',
  agent: 'Agent',
  customer: 'Customer portal',
}

function NewMemberForm({
  customers,
  customersLoading,
  saving,
  onSubmit,
  onCancel,
}: {
  customers: Customer[]
  customersLoading: boolean
  saving: boolean
  onSubmit: (payload: CreateUserPayload) => void
  onCancel: () => void
}) {
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<UserRole>('agent')
  const [customerId, setCustomerId] = useState('')

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    onSubmit({
      name: name.trim(),
      email: email.trim(),
      password,
      role,
      customer_id: role === 'customer' ? customerId : null,
    })
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <Field label="Full name" required>
        <Input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} autoFocus className="bg-surface" />
      </Field>
      <Field label="Email address" required>
        <Input type="email" value={email} onChange={(event) => setEmail(event.target.value)} className="bg-surface" />
      </Field>
      <Field label="Initial password" hint="At least 12 characters. Share it with the new user through a secure channel." required>
        <Input type="password" value={password} onChange={(event) => setPassword(event.target.value)} minLength={12} maxLength={128} autoComplete="new-password" className="bg-surface" />
      </Field>
      <Field label="Role" hint="The role controls which parts of the workspace this account can access." required>
        <Select value={role} onChange={(event) => setRole(event.target.value as UserRole)} className="bg-surface">
          {USER_ROLES.map((value) => <option key={value} value={value}>{ROLE_LABELS[value]}</option>)}
        </Select>
      </Field>
      {role === 'customer' && (
        <Field label="Linked customer" hint="Portal accounts can only view tickets belonging to this customer record." required>
          <Select value={customerId} onChange={(event) => setCustomerId(event.target.value)} disabled={customersLoading} placeholder={customersLoading ? 'Loading customers…' : 'Choose a customer'} className="bg-surface">
            {customers.map((customer) => <option key={customer.id} value={customer.id}>{customer.name} · {customer.email}</option>)}
          </Select>
        </Field>
      )}
      <div className="rounded-xl border border-info-border bg-info-subtle px-3 py-2.5 text-xs leading-relaxed text-info-text">
        The backend does not send invitations. The user will sign in with this email and initial password.
      </div>
      <div className="flex justify-end gap-2 border-t border-border-subtle pt-4">
        <Button variant="ghost" onClick={onCancel} disabled={saving}>Cancel</Button>
        <Button type="submit" loading={saving} disabled={!name.trim() || !email.trim() || password.length < 12 || (role === 'customer' && !customerId)}>
          Create account
        </Button>
      </div>
    </form>
  )
}

function MemberRow({
  member,
  currentUserId,
  canChangeRole,
  canDeactivate,
  saving,
  onChangeRole,
  onDeactivate,
}: {
  member: User
  currentUserId: string
  canChangeRole: boolean
  canDeactivate: boolean
  saving: boolean
  onChangeRole: (role: UserRole) => void
  onDeactivate: () => void
}) {
  const [roleDraft, setRoleDraft] = useState<{ memberId: string; role: UserRole } | null>(null)
  const selectedRole = roleDraft?.memberId === member.id ? roleDraft.role : member.role
  const roleChanged = selectedRole !== member.role
  const showRoleEditor = canChangeRole && member.id !== currentUserId && member.role !== 'customer'

  return (
    <div className="grid gap-3 px-4 py-4 sm:grid-cols-[minmax(0,1.2fr)_minmax(9rem,0.7fr)_auto] sm:items-center sm:px-5">
      <div className="flex min-w-0 items-center gap-3">
        <Avatar name={member.name} size="md" />
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-ink">{member.name}{member.id === currentUserId && <span className="ml-2 text-[0.6875rem] font-normal text-ink-subtle">You</span>}</p>
          <p className="truncate text-xs text-ink-muted">{member.email}</p>
          {member.role === 'customer' && <p className="mt-1 truncate text-[0.625rem] text-ink-subtle">Portal customer · linked profile</p>}
        </div>
      </div>
      <div className="flex items-center gap-2 pl-11 sm:pl-0">
        {showRoleEditor ? (
          <>
            <Select value={selectedRole} onChange={(event) => setRoleDraft({ memberId: member.id, role: event.target.value as UserRole })} aria-label={`Role for ${member.name}`} className="bg-surface">
            {STAFF_ROLES.map((role) => <option key={role} value={role}>{ROLE_LABELS[role]}</option>)}
            </Select>
            {roleChanged && <Button variant="secondary" size="sm" loading={saving} aria-label={`Save role for ${member.name}`} onClick={() => onChangeRole(selectedRole)}>Save</Button>}
          </>
        ) : <Badge variant="neutral" size="sm">{ROLE_LABELS[member.role]}</Badge>}
      </div>
      <div className="flex items-center justify-between gap-3 pl-11 sm:justify-end sm:pl-0">
        <Badge variant={member.is_active ? 'success' : 'neutral'} size="sm" dot>{member.is_active ? 'Active' : 'Inactive'}</Badge>
        {canDeactivate && member.is_active && member.id !== currentUserId && (
          <Button variant="ghost" size="sm" className="text-danger-text hover:text-danger-text" disabled={saving} onClick={onDeactivate}>Deactivate</Button>
        )}
      </div>
    </div>
  )
}

export function TeamPage() {
  const user = useSessionStore((state) => state.user)
  const queryClient = useQueryClient()
  const [offset, setOffset] = useState(0)
  const [portalFilter, setPortalFilter] = useState<'all' | 'staff' | 'customers'>('all')
  const [createOpen, setCreateOpen] = useState(false)
  const [deactivating, setDeactivating] = useState<User | null>(null)
  const [createdCredentials, setCreatedCredentials] = useState<{ member: User; password: string } | null>(null)
  const canCreate = can(user?.role, PERMISSIONS.USER_CREATE)
  const canChangeRole = can(user?.role, PERMISSIONS.USER_UPDATE_ROLE)
  const canDeactivate = can(user?.role, PERMISSIONS.USER_DEACTIVATE)

  const membersQuery = useQuery({
    queryKey: teamKeys.list({ limit: PAGE_SIZE, offset }),
    queryFn: async () => (await apiClient.get<User[]>('/users', { params: { limit: PAGE_SIZE, offset } })).data,
  })
  const customersQuery = useQuery({
    queryKey: customerKeys.list({ limit: 100, offset: 0 }),
    queryFn: async () => (await apiClient.get<Customer[]>('/customers', { params: { limit: 100, offset: 0 } })).data,
    enabled: canCreate,
  })

  const invalidateTeam = () => {
    void queryClient.invalidateQueries({ queryKey: teamKeys.lists() })
    void queryClient.invalidateQueries({ queryKey: customerKeys.all })
  }
  const createMutation = useMutation<User, unknown, CreateUserPayload>({
    mutationFn: async (payload) => (await apiClient.post<User>('/users', payload)).data,
    onSuccess: (member, variables) => {
      invalidateTeam()
      setCreateOpen(false)
      setCreatedCredentials({ member, password: variables.password })
      toast.success('Account created', `${member.name} can now sign in with the credentials you set.`)
    },
    onError: (error) => toast.error('Account could not be created', toApiError(error).message),
  })
  const roleMutation = useMutation<User, unknown, { id: string; role: UserRole }>({
    mutationFn: async ({ id, role }) => (await apiClient.patch<User>(`/users/${id}/role`, { role })).data,
    onSuccess: (member) => {
      invalidateTeam()
      toast.success('Role updated', `${member.name} is now a ${ROLE_LABELS[member.role].toLowerCase()}.`)
    },
    onError: (error) => toast.error('Role could not be changed', toApiError(error).message),
  })
  const deactivateMutation = useMutation<User, unknown, string>({
    mutationFn: async (id) => (await apiClient.post<User>(`/users/${id}/deactivate`)).data,
    onSuccess: (member) => {
      invalidateTeam()
      setDeactivating(null)
      toast.success('Account deactivated', `${member.name} can no longer sign in.`)
    },
    onError: (error) => toast.error('Account could not be deactivated', toApiError(error).message),
  })

  if (!user) return null
  const allMembers = membersQuery.data ?? []
  const visibleMembers = allMembers.filter((member) =>
    portalFilter === 'all' || (portalFilter === 'customers' ? member.role === 'customer' : member.role !== 'customer'),
  )

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <PageHeader
          title="Team"
          description="Manage workspace access and customer portal accounts."
          actions={canCreate ? <Button leadingIcon={<UserPlus aria-hidden="true" />} onClick={() => setCreateOpen(true)}>Add team member</Button> : undefined}
        />

        <Card variant="bezel" className="overflow-hidden">
          <div className="flex flex-col gap-3 border-b border-border-subtle bg-surface-sunken/35 p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5">
            <div className="flex rounded-xl border border-border-subtle bg-surface p-1" aria-label="Filter team members">
              {([
                ['all', 'Everyone'],
                ['staff', 'Staff'],
                ['customers', 'Portal accounts'],
              ] as const).map(([key, label]) => (
                <button key={key} type="button" aria-pressed={portalFilter === key} onClick={() => setPortalFilter(key)} className={`rounded-lg px-3 py-2 text-xs font-medium transition-colors ${portalFilter === key ? 'bg-ink text-canvas' : 'text-ink-muted hover:text-ink'}`}>
                  {label}
                </button>
              ))}
            </div>
            <p className="text-xs text-ink-subtle">{membersQuery.data ? `${visibleMembers.length} accounts on this page` : 'Organization access'}</p>
          </div>

          {membersQuery.isPending ? (
            <div className="divide-y divide-border-subtle" aria-label="Loading team members">
              {Array.from({ length: 6 }, (_, index) => <div key={index} className="flex items-center gap-3 px-5 py-4"><Skeleton className="size-8 rounded-full" /><div className="flex flex-1 flex-col gap-2"><Skeleton className="h-3 w-1/3" /><Skeleton className="h-3 w-1/2" /></div></div>)}
            </div>
          ) : membersQuery.error ? (
            <ErrorState title="Team members couldn’t load" error={membersQuery.error} onRetry={() => { void membersQuery.refetch() }} />
          ) : visibleMembers.length ? (
            <div className="divide-y divide-border-subtle">
              {visibleMembers.map((member) => (
                <MemberRow
                  key={member.id}
                  member={member}
                  currentUserId={user.id}
                  canChangeRole={canChangeRole}
                  canDeactivate={canDeactivate}
                  saving={roleMutation.isPending || deactivateMutation.isPending}
                  onChangeRole={(role) => roleMutation.mutate({ id: member.id, role })}
                  onDeactivate={() => setDeactivating(member)}
                />
              ))}
            </div>
          ) : (
            <EmptyState icon={<UsersThree aria-hidden="true" />} title="No accounts in this view" description="Try another filter to see organization accounts." />
          )}

          {!membersQuery.isPending && !membersQuery.error && allMembers.length > 0 && (
            <div className="flex items-center justify-between border-t border-border-subtle px-4 py-3 sm:px-5">
              <p className="text-xs text-ink-subtle">Showing {offset + 1}–{offset + allMembers.length}</p>
              <div className="flex gap-2">
                <Button variant="secondary" size="sm" disabled={offset === 0} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>Previous</Button>
                <Button variant="secondary" size="sm" disabled={allMembers.length < PAGE_SIZE} onClick={() => setOffset((value) => value + PAGE_SIZE)}>Next</Button>
              </div>
            </div>
          )}
        </Card>
      </div>

      <Dialog open={createOpen} onClose={() => setCreateOpen(false)} title="Create an account" description="Add a staff member or link a customer to the portal.">
        {createMutation.error != null && <p role="alert" className="mb-4 text-sm text-danger-text">{toApiError(createMutation.error).message}</p>}
        {createOpen && <NewMemberForm customers={customersQuery.data ?? []} customersLoading={customersQuery.isPending} saving={createMutation.isPending} onSubmit={(payload) => createMutation.mutate(payload)} onCancel={() => setCreateOpen(false)} />}
      </Dialog>

      <Dialog open={createdCredentials !== null} onClose={() => setCreatedCredentials(null)} title="Account is ready" description="Share these sign-in details with the new account holder through a secure channel.">
        {createdCredentials && (
          <div className="flex flex-col gap-4">
            <p className="text-sm text-ink">{createdCredentials.member.name} · {ROLE_LABELS[createdCredentials.member.role]}</p>
            <Field label="Email address"><Input readOnly value={createdCredentials.member.email} className="bg-surface-sunken" /></Field>
            <Field label="Initial password"><Input readOnly value={createdCredentials.password} className="bg-surface-sunken" /></Field>
            <Button
              variant="secondary"
              size="sm"
              className="w-fit"
              leadingIcon={<Copy aria-hidden="true" />}
              onClick={() => {
                void navigator.clipboard.writeText(createdCredentials.password)
                  .then(() => toast.success('Password copied', 'Share it through a secure channel.'))
                  .catch(() => toast.error('Copy failed', 'Select and copy the password manually.'))
              }}
            >
              Copy initial password
            </Button>
            <p className="text-xs leading-relaxed text-ink-subtle">This password is shown only now. Store or share it securely; the app does not send invitations or reset links yet.</p>
            <div className="flex justify-end">
              <Button leadingIcon={<Check aria-hidden="true" />} onClick={() => setCreatedCredentials(null)}>Done</Button>
            </div>
          </div>
        )}
      </Dialog>

      <ConfirmDialog
        open={deactivating !== null}
        onClose={() => setDeactivating(null)}
        title={`Deactivate ${deactivating?.name ?? 'this account'}?`}
        description="They will lose access to the workspace, but their ticket and audit history will remain. This action cannot be reversed from the current API."
        confirmLabel="Deactivate account"
        variant="danger"
        loading={deactivateMutation.isPending}
        onConfirm={() => { if (deactivating) deactivateMutation.mutate(deactivating.id) }}
      />
    </AppShell>
  )
}
