import { useQuery } from '@tanstack/react-query'

import { apiClient } from '@/services/api/client'

interface ReadinessResponse {
  status: 'ready' | 'degraded'
  checks: Record<string, boolean>
}

/**
 * The readiness probe, at `/dev/health` in development only.
 *
 * It began as the Phase C scaffold's only page, at `/`, whose job was to prove
 * the full local loop end to end: Vite dev server → proxy → FastAPI → readiness
 * probe. The landing page took the root in F2 and this moved under the
 * development routes rather than being deleted, because that job is not
 * finished — it is still the one screen that shows whether the API, the database
 * and the object store are actually up, with nothing between it and the server.
 *
 * F8 moves this panel into the admin screen, which is where a status readout
 * belongs once there is a product around it, and deletes this page.
 */
export function HealthPage() {
  const { data, isPending, isError } = useQuery({
    queryKey: ['health'],
    queryFn: async () => {
      // Absolute path: health sits outside the versioned API prefix that
      // apiClient's baseURL points at.
      const response = await apiClient.get<ReadinessResponse>('/health/ready', {
        baseURL: import.meta.env.VITE_API_BASE_URL ?? '',
      })
      return response.data
    },
  })

  return (
    <main className="grid min-h-dvh place-items-center p-6">
      <section className="w-full max-w-md rounded-xl border border-border-subtle bg-surface p-8 shadow-sm">
        <h1 className="text-2xl font-semibold tracking-tight">SupportFlow</h1>
        <p className="mt-1 text-sm text-ink-muted">
          AI-assisted customer support, built for modern support teams.
        </p>

        <div className="mt-6 border-t border-border-subtle pt-4">
          <h2 className="text-xs font-medium uppercase tracking-wide text-ink-muted">
            Backend status
          </h2>

          <output className="mt-2 block text-sm" aria-live="polite">
            {isPending && <span className="text-ink-muted">Checking…</span>}

            {isError && (
              <span className="text-priority-urgent">
                Unreachable — is the API running on port 8000?
              </span>
            )}

            {data && (
              <ul className="space-y-1">
                <li className="font-medium">
                  {data.status === 'ready' ? 'Ready' : 'Degraded'}
                </li>
                {Object.entries(data.checks).map(([name, healthy]) => (
                  <li key={name} className="flex justify-between text-ink-muted">
                    <span className="capitalize">{name}</span>
                    <span>{healthy ? 'up' : 'down'}</span>
                  </li>
                ))}
              </ul>
            )}
          </output>
        </div>
      </section>
    </main>
  )
}
