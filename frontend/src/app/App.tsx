import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MotionConfig } from 'motion/react'
import { RouterProvider } from 'react-router-dom'

import { IconProvider } from '@/app/IconProvider'
import { ThemeProvider } from '@/app/ThemeProvider'
import { Toaster } from '@/components/ui/Toaster'
import { router } from '@/routes/router'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Support data changes often; WebSocket events drive invalidation, so
      // aggressive refetch-on-focus would mostly be redundant traffic.
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

/**
 * The provider stack, outside in.
 *
 * `MotionConfig reducedMotion="user"` is the outermost of the ones that matter
 * behaviourally: the `motion` library animates in JavaScript and cannot read a
 * CSS media query, so this is the only thing that makes it honour
 * `prefers-reduced-motion`. The stylesheet's own `@media` block covers
 * everything CSS animates; neither mechanism covers the other's half.
 *
 * `Toaster` sits beside the router rather than inside it because the toast
 * store is deliberately outside React's data flow — an axios interceptor and a
 * WebSocket handler both push to it, and neither has a component to call from.
 */
export function App() {
  return (
    <MotionConfig reducedMotion="user">
      <ThemeProvider>
        <IconProvider>
          <QueryClientProvider client={queryClient}>
            <RouterProvider router={router} />
            <Toaster />
          </QueryClientProvider>
        </IconProvider>
      </ThemeProvider>
    </MotionConfig>
  )
}
