import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { HealthPage } from './HealthPage'

vi.mock('@/services/api/client', () => ({
  apiClient: {
    get: vi.fn().mockResolvedValue({
      data: { status: 'ready', checks: { database: true, redis: true } },
    }),
  },
}))

function renderWithQuery(ui: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

describe('HealthPage', () => {
  it('renders the product name', () => {
    renderWithQuery(<HealthPage />)

    expect(screen.getByRole('heading', { level: 1, name: 'SupportFlow' })).toBeVisible()
  })

  it('reports backend readiness once the probe resolves', async () => {
    renderWithQuery(<HealthPage />)

    expect(await screen.findByText('Ready')).toBeVisible()
    expect(screen.getByText('database')).toBeVisible()
  })
})
