/** Query keys for notification data, shared by the socket and notification screens. */
export interface NotificationListFilters {
  unread_only?: boolean
  limit?: number
  offset?: number
}

export const notificationKeys = {
  all: ['notifications'] as const,
  lists: () => [...notificationKeys.all, 'list'] as const,
  list: (filters: NotificationListFilters = {}) =>
    [...notificationKeys.lists(), filters] as const,
  unreadCount: () => [...notificationKeys.all, 'unread-count'] as const,
}
