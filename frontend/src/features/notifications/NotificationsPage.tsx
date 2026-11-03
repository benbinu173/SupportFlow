import { Bell, Check, CheckCircle, Ticket } from '@phosphor-icons/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Skeleton } from '@/components/ui/Skeleton'
import { notificationKeys } from '@/features/notifications/keys'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { toApiError } from '@/services/api/errors'
import { useSessionStore } from '@/store/auth'
import { toast } from '@/store/toasts'
import type { components } from '@/types/api'

type Notification = components['schemas']['NotificationRead']
type NotificationType = components['schemas']['NotificationType']
type UnreadCount = components['schemas']['NotificationUnreadCount']
const PAGE_SIZE = 50

const TYPE_LABELS: Record<NotificationType, string> = {
  ticket_assigned: 'Assignment',
  ticket_reassigned: 'Reassignment',
  new_customer_reply: 'Customer reply',
  mention: 'Mention',
  sla_warning: 'SLA warning',
  sla_breached: 'SLA breach',
  ai_analysis_completed: 'AI update',
  ticket_resolved: 'Resolved',
}

function formatNotificationTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

export function NotificationsPage() {
  const user = useSessionStore((state) => state.user)
  const queryClient = useQueryClient()
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [offset, setOffset] = useState(0)
  const filters = { unread_only: unreadOnly, limit: PAGE_SIZE, offset }

  const notificationsQuery = useQuery({
    queryKey: notificationKeys.list(filters),
    queryFn: async () => (await apiClient.get<Notification[]>('/notifications', { params: filters })).data,
  })

  const markReadMutation = useMutation<Notification, unknown, string>({
    mutationFn: async (id) => (await apiClient.post<Notification>(`/notifications/${id}/read`)).data,
    onSuccess: (updated) => {
      queryClient.setQueriesData<Notification[]>({ queryKey: notificationKeys.lists() }, (current) =>
        current?.map((notification) => notification.id === updated.id ? updated : notification),
      )
      void queryClient.invalidateQueries({ queryKey: notificationKeys.unreadCount() })
      void queryClient.invalidateQueries({ queryKey: notificationKeys.lists() })
    },
    onError: (error) => toast.error('Notification could not be marked read', toApiError(error).message),
  })

  const markAllMutation = useMutation<{ marked_read: number }, unknown, void>({
    mutationFn: async () => (await apiClient.post<{ marked_read: number }>('/notifications/read-all')).data,
    onSuccess: ({ marked_read }) => {
      const readAt = new Date().toISOString()
      queryClient.setQueriesData<Notification[]>({ queryKey: notificationKeys.lists() }, (current) =>
        current?.map((notification) => notification.read_at === null ? { ...notification, read_at: readAt } : notification),
      )
      queryClient.setQueryData<UnreadCount>(notificationKeys.unreadCount(), { unread: 0 })
      void queryClient.invalidateQueries({ queryKey: notificationKeys.lists() })
      toast.success('Inbox cleared', `${marked_read} notification${marked_read === 1 ? '' : 's'} marked as read.`)
    },
    onError: (error) => toast.error('Inbox could not be updated', toApiError(error).message),
  })

  if (!user) return null
  const notifications = notificationsQuery.data ?? []
  const hasNextPage = notifications.length === PAGE_SIZE

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-4xl flex-col gap-6">
        <PageHeader
          title="Notifications"
          description="Updates about tickets and activity that needs your attention."
          actions={(
            <Button
              variant="secondary"
              leadingIcon={<CheckCircle aria-hidden="true" />}
              disabled={markAllMutation.isPending}
              loading={markAllMutation.isPending}
              onClick={() => markAllMutation.mutate()}
            >
              Mark all read
            </Button>
          )}
        />

        <Card variant="bezel" className="overflow-hidden">
          <div className="flex items-center justify-between gap-3 border-b border-border-subtle bg-surface-sunken/35 p-4 sm:px-5">
            <div className="flex rounded-xl border border-border-subtle bg-surface p-1" role="group" aria-label="Filter notifications">
              <button type="button" aria-pressed={!unreadOnly} onClick={() => { setUnreadOnly(false); setOffset(0) }} className={`rounded-lg px-3 py-2 text-xs font-medium transition-colors ${!unreadOnly ? 'bg-ink text-canvas' : 'text-ink-muted hover:text-ink'}`}>All</button>
              <button type="button" aria-pressed={unreadOnly} onClick={() => { setUnreadOnly(true); setOffset(0) }} className={`rounded-lg px-3 py-2 text-xs font-medium transition-colors ${unreadOnly ? 'bg-ink text-canvas' : 'text-ink-muted hover:text-ink'}`}>Unread</button>
            </div>
            <span className="text-xs text-ink-subtle">Newest first</span>
          </div>

          {notificationsQuery.isPending ? (
            <div className="divide-y divide-border-subtle" aria-label="Loading notifications">
              {Array.from({ length: 5 }, (_, index) => <div key={index} className="flex gap-3 px-5 py-5"><Skeleton className="size-9 rounded-xl" /><div className="flex flex-1 flex-col gap-2"><Skeleton className="h-3 w-2/5" /><Skeleton className="h-3 w-4/5" /></div></div>)}
            </div>
          ) : notificationsQuery.error ? (
            <ErrorState title="Notifications couldn’t load" error={notificationsQuery.error} onRetry={() => { void notificationsQuery.refetch() }} />
          ) : notifications.length ? (
            <div className="divide-y divide-border-subtle">
              {notifications.map((notification) => {
                const unread = notification.read_at === null
                return (
                  <article key={notification.id} className={`flex items-start gap-3 px-4 py-4 sm:px-5 ${unread ? 'bg-brand-subtle/25' : ''}`}>
                    <span className={`grid size-9 shrink-0 place-items-center rounded-xl ${unread ? 'bg-brand-subtle text-brand-text' : 'bg-surface-sunken text-ink-subtle'}`}>
                      {notification.ticket_id ? <Ticket aria-hidden="true" className="size-4" /> : <Bell aria-hidden="true" className="size-4" />}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className={`text-sm ${unread ? 'font-semibold text-ink' : 'font-medium text-ink-muted'}`}>{notification.title}</p>
                        <Badge variant={unread ? 'brand' : 'neutral'} size="sm">{TYPE_LABELS[notification.notification_type]}</Badge>
                      </div>
                      <p className="mt-1 text-xs leading-relaxed text-ink-muted">{notification.body}</p>
                      <time dateTime={notification.created_at} className="mt-2 block text-[0.6875rem] text-ink-subtle">{formatNotificationTime(notification.created_at)}</time>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {notification.ticket_id && (
                        <Link to={`/tickets/${notification.ticket_id}`} className="rounded-md px-2 py-1.5 text-xs font-medium text-brand-text hover:bg-brand-subtle">View ticket</Link>
                      )}
                      {unread && (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="shrink-0 px-2"
                          aria-label={`Mark ${notification.title} as read`}
                          loading={markReadMutation.isPending && markReadMutation.variables === notification.id}
                          onClick={() => markReadMutation.mutate(notification.id)}
                          leadingIcon={<Check aria-hidden="true" />}
                        >
                          <span className="hidden sm:inline">Mark read</span>
                        </Button>
                      )}
                    </div>
                  </article>
                )
              })}
            </div>
          ) : (
            <EmptyState
              icon={<Bell aria-hidden="true" />}
              title={unreadOnly ? 'You’re all caught up' : 'No notifications yet'}
              description={unreadOnly ? 'New unread updates will appear here.' : 'Ticket assignments, replies, and important updates will show up here.'}
            />
          )}

          {!notificationsQuery.isPending && !notificationsQuery.error && notifications.length > 0 && (
            <div className="flex items-center justify-between border-t border-border-subtle px-4 py-3 sm:px-5">
              <p className="text-xs text-ink-subtle">Showing {offset + 1}–{offset + notifications.length}</p>
              <div className="flex gap-2">
                <Button variant="secondary" size="sm" disabled={offset === 0} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>Previous</Button>
                <Button variant="secondary" size="sm" disabled={!hasNextPage} onClick={() => setOffset((value) => value + PAGE_SIZE)}>Next</Button>
              </div>
            </div>
          )}
        </Card>
      </div>
    </AppShell>
  )
}
