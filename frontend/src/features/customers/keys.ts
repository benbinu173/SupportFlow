/** Query keys for customer records linked to tickets and customer screens. */
export const customerKeys = {
  all: ['customers'] as const,
  details: () => [...customerKeys.all, 'detail'] as const,
  detail: (customerId: string) => [...customerKeys.details(), customerId] as const,
}
