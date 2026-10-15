import { Outlet, createBrowserRouter, type RouteObject } from 'react-router-dom'

import { HealthPage } from '@/pages/HealthPage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { KitPage } from '@/pages/dev/KitPage'
import { RouteError } from '@/routes/RouteError'

/**
 * Development-only routes.
 *
 * Gated on `import.meta.env.DEV` rather than left in and hidden, because a
 * component kit reachable in production is a page with a fixture user in it.
 * Vite replaces the flag with `false` at build time, so the branch and the
 * import behind it are both dropped from the shipped bundle.
 */
const devRoutes: RouteObject[] = import.meta.env.DEV
  ? [{ path: '/dev/kit', element: <KitPage /> }]
  : []

/**
 * The route table.
 *
 * The root entry is pathless: it renders nothing of its own and exists to carry
 * the error element over every route at once, and to give later phases a single
 * place to hang the auth bootstrap and the realtime provider without touching
 * each screen.
 *
 * `/` is still the scaffold's health page. It becomes the marketing landing
 * page in F2, at which point the health surface moves to the admin screen and
 * this scaffold page is deleted.
 */
export const router = createBrowserRouter([
  {
    element: <Outlet />,
    errorElement: <RouteError />,
    children: [
      {
        path: '/',
        element: <HealthPage />,
      },
      ...devRoutes,
      {
        path: '*',
        element: <NotFoundPage />,
      },
    ],
  },
])
