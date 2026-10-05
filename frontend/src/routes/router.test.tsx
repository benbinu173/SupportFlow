import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

import { apiClient } from '@/services/api/client'

import { router } from './router'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

const get = apiClient.get as unknown as Mock

beforeEach(() => {
  get.mockReset()
  get.mockResolvedValue({ data: { status: 'ready', checks: {} } })
})

/**
 * The route table, rendered through a memory router built from the *same* route objects the
 * application uses. Asserting on `router.routes[0].path` would restate the config; this
 * navigates to a URL and checks which screen answers, which is the thing a user experiences.
 */
function renderAt(path: string) {
  const memoryRouter = createMemoryRouter(router.routes, { initialEntries: [path] })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={memoryRouter} />
    </QueryClientProvider>
  )
}

describe('route table', () => {
  it('serves the health page at the root', async () => {
    renderAt('/')

    expect(await screen.findByRole('heading', { level: 1, name: 'SupportFlow' })).toBeVisible()
    expect(await screen.findByText('Ready')).toBeVisible()
  })

  it('has nothing else mounted yet', () => {
    // One route is the current truth, not an oversight: role-scoped sections arrive with the
    // screens behind them. This fails the moment a route is added without a test, which is the
    // point — a growing route table with a one-line test file is how a guard gets skipped.
    expect(router.routes).toHaveLength(1)
  })
})
