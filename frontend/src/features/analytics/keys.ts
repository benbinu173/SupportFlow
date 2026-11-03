export const analyticsKeys = {
  all: ['analytics'] as const,
  overview: (range?: string) => [...analyticsKeys.all, 'overview', range ?? 'default'] as const,
  tickets: (range?: string) => [...analyticsKeys.all, 'tickets', range ?? 'default'] as const,
  agents: (range?: string) => [...analyticsKeys.all, 'agents', range ?? 'default'] as const,
  sla: (range?: string) => [...analyticsKeys.all, 'sla', range ?? 'default'] as const,
  sentiment: (range?: string) => [...analyticsKeys.all, 'sentiment', range ?? 'default'] as const,
}
