import { ArrowLeft, PaperPlaneRight, ShieldCheck, Ticket } from '@phosphor-icons/react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { Field } from '@/components/ui/Field'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { ticketKeys } from '@/features/tickets/keys'
import { useSessionStore } from '@/store/auth'
import { toast } from '@/store/toasts'
import { toApiError } from '@/services/api/errors'
import type { components } from '@/types/api'

type TicketRead = components['schemas']['TicketRead']

export function NewRequestPage() {
  const user = useSessionStore((state) => state.user)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [subject, setSubject] = useState('')
  const [description, setDescription] = useState('')

  const createMutation = useMutation<TicketRead, unknown, { subject: string; description: string }>({
    mutationFn: async (payload) => (await apiClient.post<TicketRead>('/tickets', payload)).data,
    onSuccess: (ticket) => {
      queryClient.setQueryData(ticketKeys.detail(ticket.id), ticket)
      void queryClient.invalidateQueries({ queryKey: ticketKeys.lists() })
      toast.success('Request submitted', 'Your support conversation is ready.')
      navigate(`/tickets/${ticket.id}`)
    },
    onError: (error) => toast.error('Request could not be submitted', toApiError(error).message),
  })

  if (!user) return null

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const cleanSubject = subject.trim()
    const cleanDescription = description.trim()
    if (!cleanSubject || !cleanDescription || createMutation.isPending) return
    createMutation.mutate({ subject: cleanSubject, description: cleanDescription })
  }

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-5xl flex-col gap-6">
        <Button variant="ghost" size="sm" className="w-fit px-0" leadingIcon={<ArrowLeft aria-hidden="true" />} onClick={() => navigate('/portal')}>
          Back to your requests
        </Button>

        <PageHeader
          title="Tell us what you need"
          description="Share a few details and our support team will pick it up. You can continue the conversation from your request page."
          actions={<Badge variant="brand" size="md" dot>Private to your account</Badge>}
        />

        <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_18rem]">
          <Card variant="bezel" className="overflow-hidden">
            <form onSubmit={submit}>
              <div className="flex items-center gap-3 border-b border-border-subtle px-5 py-4">
                <span className="grid size-9 place-items-center rounded-xl bg-brand-subtle text-brand-text">
                  <Ticket aria-hidden="true" className="size-4" />
                </span>
                <div>
                  <h2 className="text-sm font-semibold text-ink">New support request</h2>
                  <p className="mt-0.5 text-xs text-ink-muted">Fields marked required help us route your request.</p>
                </div>
              </div>

              <div className="flex flex-col gap-5 p-5 sm:p-6">
                <Field
                  label="What can we help with?"
                  hint="A short description makes your request easier to spot."
                  required
                  error={subject.length > 0 && !subject.trim() ? 'Add a short subject.' : undefined}
                >
                  <Input
                    value={subject}
                    onChange={(event) => setSubject(event.target.value)}
                    placeholder="For example, I can’t access my account"
                    maxLength={500}
                    autoFocus
                    className="h-11 bg-surface"
                  />
                </Field>

                <Field
                  label="Describe what happened"
                  hint="Include what you were trying to do and any details that might help. Please avoid sharing passwords or payment card numbers."
                  required
                  error={description.length > 0 && !description.trim() ? 'Add some details about your request.' : undefined}
                >
                  <Textarea
                    value={description}
                    onChange={(event) => setDescription(event.target.value)}
                    placeholder="Write your message to the support team…"
                    maxLength={20_000}
                    rows={8}
                    className="min-h-48 bg-surface"
                  />
                </Field>

                {createMutation.error != null && (
                  <div role="alert" className="rounded-xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger-text">
                    {toApiError(createMutation.error).message}
                  </div>
                )}

                <div className="flex flex-col-reverse gap-3 border-t border-border-subtle pt-4 sm:flex-row sm:items-center sm:justify-between">
                  <p className="text-xs text-ink-subtle">You can add files and reply after submitting.</p>
                  <Button
                    type="submit"
                    loading={createMutation.isPending}
                    disabled={!subject.trim() || !description.trim()}
                    leadingIcon={<PaperPlaneRight aria-hidden="true" weight="bold" />}
                  >
                    Submit request
                  </Button>
                </div>
              </div>
            </form>
          </Card>

          <Card className="overflow-hidden">
            <div className="flex items-center gap-2 border-b border-border-subtle px-4 py-3.5">
              <ShieldCheck aria-hidden="true" className="size-4 text-success-text" />
              <h2 className="text-sm font-semibold text-ink">Your request stays private</h2>
            </div>
            <div className="flex flex-col gap-3 p-4">
              <p className="text-xs leading-relaxed text-ink-muted">
                Only you and the support team handling your request can see this conversation. Replies and status changes will appear in your request history.
              </p>
              <div className="rounded-xl bg-surface-sunken px-3 py-3 text-[0.6875rem] leading-relaxed text-ink-subtle">
                Do not include passwords, one-time codes, or full payment details in your message.
              </div>
            </div>
          </Card>
        </div>
      </div>
    </AppShell>
  )
}
