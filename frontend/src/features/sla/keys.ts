export const slaKeys = {
  all: ['sla'] as const,
  policies: () => [...slaKeys.all, 'policies'] as const,
  overview: () => [...slaKeys.all, 'overview'] as const,
}
