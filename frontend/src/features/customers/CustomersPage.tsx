import { ArrowRight, MagnifyingGlass, UserPlus, Users } from '@phosphor-icons/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { Avatar } from '@/components/ui/Avatar'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { Dialog } from '@/components/ui/Dialog'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { CustomerForm, type CustomerFormValues } from '@/features/customers/CustomerForm'
import { customerKeys } from '@/features/customers/keys'
import { can, PERMISSIONS } from '@/features/auth/permissions'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { toApiError } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import { toast } from '@/store/toasts'
import type { components } from '@/types/api'

type Customer = components['schemas']['CustomerRead']
const PAGE_SIZE = 50

function CustomerRow({ customer }: { customer: Customer }) {
  return (
    <Link
      to={`/customers/${customer.id}`}
      className="group grid gap-3 px-4 py-4 transition-colors hover:bg-surface-sunken/60 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-focus sm:grid-cols-[minmax(0,1.2fr)_minmax(12rem,1fr)_auto] sm:items-center sm:px-5"
    >
      <div className="flex min-w-0 items-center gap-3">
        <Avatar name={customer.name} size="md" />
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-ink group-hover:text-brand-text">{customer.name}</p>
          <p className="truncate text-xs text-ink-muted">{customer.email}</p>
        </div>
      </div>
      <div className="flex min-w-0 flex-col gap-1 pl-[3.25rem] sm:pl-0">
        <p className="truncate text-xs text-ink-muted">{customer.phone ?? 'No phone number'}</p>
        {customer.external_reference && <p className="truncate text-[0.6875rem] text-ink-subtle">Ref · {customer.external_reference}</p>}
      </div>
      <div className="flex items-center justify-between gap-3 pl-[3.25rem] sm:justify-end sm:pl-0">
        <Badge variant="neutral" size="sm">Customer</Badge>
        <ArrowRight aria-hidden="true" className="size-4 text-ink-subtle transition-transform group-hover:translate-x-0.5 group-hover:text-brand-text" />
      </div>
    </Link>
  )
}

export function CustomersPage() {
  const user = useSessionStore((state) => state.user)
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [committedSearch, setCommittedSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [createOpen, setCreateOpen] = useState(false)
  const canCreate = can(user?.role, PERMISSIONS.CUSTOMER_CREATE)

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setCommittedSearch(search.trim())
      setOffset(0)
    }, 250)
    return () => window.clearTimeout(timeout)
  }, [search])

  const filters = {
    ...(committedSearch ? { q: committedSearch } : {}),
    sort: 'created_at' as const,
    order: 'desc' as const,
    limit: PAGE_SIZE,
    offset,
  }
  const customersQuery = useQuery({
    queryKey: customerKeys.list(filters),
    queryFn: async () => (await apiClient.get<Customer[]>('/customers', { params: filters })).data,
  })

  const createMutation = useMutation<Customer, unknown, CustomerFormValues>({
    mutationFn: async (payload) => (await apiClient.post<Customer>('/customers', payload)).data,
    onSuccess: (customer) => {
      void queryClient.invalidateQueries({ queryKey: customerKeys.lists() })
      queryClient.setQueryData(customerKeys.detail(customer.id), customer)
      setCreateOpen(false)
      toast.success('Customer added', `${customer.name} is now in your customer directory.`)
    },
    onError: (error) => toast.error('Customer could not be added', toApiError(error).message),
  })

  if (!user) return null
  const customers = customersQuery.data ?? []

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <PageHeader
          title="Customers"
          description="Keep customer contact details close to the conversations your team supports."
          actions={canCreate ? <Button leadingIcon={<UserPlus aria-hidden="true" />} onClick={() => setCreateOpen(true)}>Add customer</Button> : undefined}
        />

        <Card variant="bezel" className="overflow-hidden">
          <div className="flex flex-col gap-3 border-b border-border-subtle bg-surface-sunken/35 p-4 sm:flex-row sm:items-center sm:p-5">
            <div className="min-w-0 flex-1">
              <Input
                type="search"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search by name or email"
                aria-label="Search customers by name or email"
                maxLength={200}
                leadingIcon={<MagnifyingGlass aria-hidden="true" />}
                className="bg-surface"
              />
            </div>
            <p className="text-xs text-ink-subtle">{customersQuery.data ? `${customers.length} on this page` : 'Customer directory'}</p>
          </div>

          {customersQuery.isPending ? (
            <div className="divide-y divide-border-subtle" aria-label="Loading customers">
              {Array.from({ length: 6 }, (_, index) => (
                <div key={index} className="flex items-center gap-3 px-5 py-4">
                  <Skeleton className="size-10 rounded-full" />
                  <div className="flex flex-1 flex-col gap-2"><Skeleton className="h-3 w-1/3" /><Skeleton className="h-3 w-1/2" /></div>
                </div>
              ))}
            </div>
          ) : customersQuery.error ? (
            <ErrorState title="Customers couldn’t load" error={customersQuery.error} onRetry={() => { void customersQuery.refetch() }} />
          ) : customers.length ? (
            <div className="divide-y divide-border-subtle">{customers.map((customer) => <CustomerRow key={customer.id} customer={customer} />)}</div>
          ) : (
            <EmptyState
              icon={<Users aria-hidden="true" />}
              title={committedSearch ? 'No matching customers' : 'Your customer directory is empty'}
              description={committedSearch ? 'Try another name or email address.' : 'Add a customer to keep their contact details connected to their support history.'}
              action={canCreate && !committedSearch ? <Button onClick={() => setCreateOpen(true)}>Add your first customer</Button> : undefined}
            />
          )}

          {!customersQuery.isPending && !customersQuery.error && customers.length > 0 && (
            <div className="flex items-center justify-between border-t border-border-subtle px-4 py-3 sm:px-5">
              <p className="text-xs text-ink-subtle">Showing {offset + 1}–{offset + customers.length}</p>
              <div className="flex gap-2">
                <Button variant="secondary" size="sm" disabled={offset === 0} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>Previous</Button>
                <Button variant="secondary" size="sm" disabled={customers.length < PAGE_SIZE} onClick={() => setOffset((value) => value + PAGE_SIZE)}>Next</Button>
              </div>
            </div>
          )}
        </Card>
      </div>

      <Dialog open={createOpen} onClose={() => setCreateOpen(false)} title="Add a customer" description="Create a support contact in your organization.">
        {createMutation.error && <p role="alert" className="mb-4 text-sm text-danger-text">{toApiError(createMutation.error).message}</p>}
        {createOpen && <CustomerForm key="new-customer" saving={createMutation.isPending} onSubmit={(values) => createMutation.mutate(values)} onCancel={() => setCreateOpen(false)} />}
      </Dialog>
    </AppShell>
  )
}
