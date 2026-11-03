import { ArrowLeft, EnvelopeSimple, PencilSimple, Phone, Ticket } from '@phosphor-icons/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { Avatar } from '@/components/ui/Avatar'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { Dialog } from '@/components/ui/Dialog'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Skeleton } from '@/components/ui/Skeleton'
import { CustomerForm, type CustomerFormValues } from '@/features/customers/CustomerForm'
import { customerKeys } from '@/features/customers/keys'
import { can, PERMISSIONS } from '@/features/auth/permissions'
import { ticketKeys } from '@/features/tickets/keys'
import { TICKET_STATUS_LABELS, TICKET_STATUS_VARIANTS } from '@/features/tickets/presentation'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { toApiError } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import { toast } from '@/store/toasts'
import type { components } from '@/types/api'

type Customer = components['schemas']['CustomerRead']
type TicketRead = components['schemas']['TicketRead']

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { month: 'long', day: 'numeric', year: 'numeric' }).format(new Date(value))
}

export function CustomerDetailPage() {
  const { customerId = '' } = useParams()
  const navigate = useNavigate()
  const user = useSessionStore((state) => state.user)
  const queryClient = useQueryClient()
  const [editOpen, setEditOpen] = useState(false)
  const canUpdate = can(user?.role, PERMISSIONS.CUSTOMER_UPDATE)

  const customerQuery = useQuery({
    queryKey: customerKeys.detail(customerId),
    queryFn: async () => (await apiClient.get<Customer>(`/customers/${customerId}`)).data,
    enabled: Boolean(customerId),
  })
  const customer = customerQuery.data
  const ticketsQuery = useQuery({
    queryKey: ticketKeys.list({ customer_id: customerId, sort: 'updated_at', order: 'desc', limit: 10, offset: 0 }),
    queryFn: async () => (await apiClient.get<TicketRead[]>('/tickets', {
      params: { customer_id: customerId, sort: 'updated_at', order: 'desc', limit: 10, offset: 0 },
    })).data,
    enabled: Boolean(customerId && customer),
  })

  const updateMutation = useMutation<Customer, unknown, CustomerFormValues>({
    mutationFn: async (payload) => (await apiClient.patch<Customer>(`/customers/${customerId}`, payload)).data,
    onSuccess: (updated) => {
      queryClient.setQueryData(customerKeys.detail(customerId), updated)
      void queryClient.invalidateQueries({ queryKey: customerKeys.lists() })
      setEditOpen(false)
      toast.success('Customer updated', `${updated.name}’s contact details are saved.`)
    },
    onError: (error) => toast.error('Customer could not be updated', toApiError(error).message),
  })

  if (!user) return null
  const error = customerQuery.error ? toApiError(customerQuery.error) : null

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <Button variant="ghost" size="sm" className="w-fit px-0" leadingIcon={<ArrowLeft aria-hidden="true" />} onClick={() => navigate('/customers')}>
          Customer directory
        </Button>

        {customerQuery.isPending ? (
          <div className="flex flex-col gap-3"><Skeleton className="h-7 w-2/5" /><Skeleton className="h-4 w-1/3" /></div>
        ) : error || !customer ? (
          <ErrorState
            title={error?.status === 404 ? 'Customer not found' : 'Customer details couldn’t load'}
            description={error?.status === 404 ? 'This customer may have been removed.' : error?.message ?? 'Try again in a moment.'}
            onRetry={() => { void customerQuery.refetch() }}
          />
        ) : (
          <>
            <PageHeader
              title={customer.name}
              description="Customer profile and support history."
              actions={canUpdate ? <Button variant="secondary" leadingIcon={<PencilSimple aria-hidden="true" />} onClick={() => setEditOpen(true)}>Edit details</Button> : undefined}
            />

            <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
              <Card variant="bezel" className="overflow-hidden">
                <div className="flex items-center gap-3 border-b border-border-subtle px-5 py-4">
                  <Avatar name={customer.name} size="lg" />
                  <div className="min-w-0">
                    <p className="truncate text-base font-semibold text-ink">{customer.name}</p>
                    <p className="text-xs text-ink-muted">Customer since {formatDate(customer.created_at)}</p>
                  </div>
                </div>
                <dl className="divide-y divide-border-subtle px-5">
                  <div className="flex items-start gap-3 py-4">
                    <EnvelopeSimple aria-hidden="true" className="mt-0.5 size-4 text-ink-subtle" />
                    <div className="min-w-0"><dt className="text-[0.6875rem] text-ink-subtle">Email</dt><dd className="mt-0.5 break-all text-sm text-ink"><a className="hover:text-brand-text" href={`mailto:${customer.email}`}>{customer.email}</a></dd></div>
                  </div>
                  <div className="flex items-start gap-3 py-4">
                    <Phone aria-hidden="true" className="mt-0.5 size-4 text-ink-subtle" />
                    <div className="min-w-0"><dt className="text-[0.6875rem] text-ink-subtle">Phone</dt><dd className="mt-0.5 text-sm text-ink">{customer.phone ? <a className="hover:text-brand-text" href={`tel:${customer.phone}`}>{customer.phone}</a> : 'Not provided'}</dd></div>
                  </div>
                  <div className="flex items-start gap-3 py-4">
                    <span aria-hidden="true" className="grid size-4 place-items-center text-[0.625rem] font-bold text-ink-subtle">ID</span>
                    <div className="min-w-0"><dt className="text-[0.6875rem] text-ink-subtle">External reference</dt><dd className="mt-0.5 text-sm text-ink">{customer.external_reference ?? 'Not provided'}</dd></div>
                  </div>
                </dl>
              </Card>

              <Card variant="bezel" className="overflow-hidden">
                <div className="flex items-center justify-between gap-3 border-b border-border-subtle px-5 py-4">
                  <div className="flex items-center gap-2.5">
                    <span className="grid size-8 place-items-center rounded-xl bg-brand-subtle text-brand-text"><Ticket aria-hidden="true" className="size-4" /></span>
                    <div><h2 className="text-sm font-semibold text-ink">Recent tickets</h2><p className="mt-0.5 text-xs text-ink-muted">Tickets visible to your role.</p></div>
                  </div>
                  {ticketsQuery.data && <Badge variant="neutral" size="sm">{ticketsQuery.data.length}</Badge>}
                </div>
                {ticketsQuery.isPending ? (
                  <div className="flex flex-col gap-4 p-5" aria-label="Loading customer tickets">
                    {Array.from({ length: 3 }, (_, index) => <div key={index} className="flex flex-col gap-2"><Skeleton className="h-3 w-1/2" /><Skeleton className="h-3 w-4/5" /></div>)}
                  </div>
                ) : ticketsQuery.error ? (
                  <ErrorState title="Tickets couldn’t load" error={ticketsQuery.error} onRetry={() => { void ticketsQuery.refetch() }} />
                ) : ticketsQuery.data?.length ? (
                  <div className="divide-y divide-border-subtle">
                    {ticketsQuery.data.map((ticket) => (
                      <Link key={ticket.id} to={`/tickets/${ticket.id}`} className="flex items-center justify-between gap-3 px-5 py-4 transition-colors hover:bg-surface-sunken/50">
                        <div className="min-w-0"><p className="text-[0.6875rem] text-ink-subtle">Ticket #{ticket.number}</p><p className="mt-1 truncate text-sm font-medium text-ink">{ticket.subject}</p></div>
                        <Badge variant={TICKET_STATUS_VARIANTS[ticket.status]} size="sm">{TICKET_STATUS_LABELS[ticket.status]}</Badge>
                      </Link>
                    ))}
                  </div>
                ) : (
                  <EmptyState icon={<Ticket aria-hidden="true" />} title="No tickets found" description="Tickets connected to this customer will appear here." className="py-12" />
                )}
              </Card>
            </div>
          </>
        )}
      </div>

      {customer && (
        <Dialog open={editOpen} onClose={() => setEditOpen(false)} title="Edit customer details" description="Update this customer’s contact information.">
          {updateMutation.error && <p role="alert" className="mb-4 text-sm text-danger-text">{toApiError(updateMutation.error).message}</p>}
          {editOpen && <CustomerForm key={customer.id} customer={customer} saving={updateMutation.isPending} onSubmit={(values) => updateMutation.mutate(values)} onCancel={() => setEditOpen(false)} />}
        </Dialog>
      )}
    </AppShell>
  )
}
