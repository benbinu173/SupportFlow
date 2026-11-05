import { Suspense } from 'react'
import { Outlet } from 'react-router-dom'

import { Spinner } from '@/components/ui/Spinner'

export function RouteOutlet() {
  return (
    <Suspense fallback={(
      <main className="grid min-h-dvh place-items-center bg-canvas">
        <Spinner size="lg" label="Loading page" />
      </main>
    )}>
      <Outlet />
    </Suspense>
  )
}
