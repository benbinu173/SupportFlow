import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

import { apiClient } from '@/services/api/client'

import { HealthPage } from './HealthPage'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

// The module mock replaces the client, so this is the same function the page calls.
// Cast loosely on purpose: the real `get` is typed to return a full `AxiosResponse`, and
// building one of those in a test means writing `status`, `headers`, and a request config
// that nothing under test reads. What the page consumes is `{ data }`.
const get = apiClient.get as unknown as Mock

function renderWithQuery(ui: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

/** Resolve the readiness probe with a body, the way the real client would. */
function responds(body: unknown) {
  get.mockResolvedValueOnce({ data: body })
}

beforeEach(() => {
  get.mockReset()
})

describe('HealthPage', () => {
  it('renders the product name', () => {
    renderWithQuery(<HealthPage />)

    expect(screen.getByRole('heading', { level: 1, name: 'SupportFlow' })).toBeVisible()
  })

  it('reports backend readiness once the probe resolves', async () => {
    responds({ status: 'ready', checks: { database: true, redis: true } })
    renderWithQuery(<HealthPage />)

    expect(await screen.findByText('Ready')).toBeVisible()
    expect(screen.getByText('database')).toBeVisible()
  })

  it('says it is checking while the probe is still outstanding', () => {
    // A promise that never settles: the pending branch is what a slow API actually looks
    // like, and it is the one state a resolved mock cannot reach.
    get.mockReturnValueOnce(new Promise(() => {}))
    renderWithQuery(<HealthPage />)

    expect(screen.getByText('Checking…')).toBeVisible()
  })

  it('names the likely cause when the API cannot be reached', async () => {
    get.mockRejectedValueOnce(new Error('connection refused'))
    renderWithQuery(<HealthPage />)

    expect(await screen.findByText(/Unreachable/)).toBeVisible()
  })

  it('distinguishes a degraded backend from a ready one', async () => {
    responds({ status: 'degraded', checks: { database: true, redis: false } })
    renderWithQuery(<HealthPage />)

    expect(await screen.findByText('Degraded')).toBeVisible()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
  })

  it('shows each dependency the probe covered, and whether it is up', async () => {
    responds({ status: 'degraded', checks: { database: true, redis: false } })
    renderWithQuery(<HealthPage />)

    await screen.findByText('Degraded')
    expect(screen.getByText('database')).toBeVisible()
    expect(screen.getByText('redis')).toBeVisible()
    expect(screen.getByText('up')).toBeVisible()
    expect(screen.getByText('down')).toBeVisible()
  })

  it('asks the readiness probe outside the versioned API prefix', async () => {
    responds({ status: 'ready', checks: {} })
    renderWithQuery(<HealthPage />)

    await screen.findByText('Ready')
    // Health sits outside `/api/v1`, which is what `apiClient`'s baseURL points at, so the
    // page has to override it. A regression here is a 404 that only shows up at runtime.
    expect(get).toHaveBeenCalledWith('/health/ready', { baseURL: expect.any(String) })
  })
})
