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

  it('serves the component kit on the development route', async () => {
    renderAt('/dev/kit')

    expect(await screen.findByRole('heading', { level: 1, name: 'Component kit' })).toBeVisible()
  })

  it('renders the not-found page for an unmatched path', async () => {
    renderAt('/no/such/place')

    expect(await screen.findByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible()
  })

  it('mounts every route the tests above cover, and no others', () => {
    // The inventory is the guard. A route added without a case here fails this test, which is
    // the point: a growing route table beside a one-line test file is how a guard gets skipped.
    // The paths are asserted rather than the count, so the failure names what was added.
    //
    // `/dev/kit` is present because `import.meta.env.DEV` is true under Vitest; the production
    // build drops it, along with the page behind it.
    const rootRoute = router.routes[0]
    const childPaths = (rootRoute?.children ?? []).map((route) => route.path)

    expect(childPaths).toEqual(['/', '/dev/kit', '*'])
  })
})
