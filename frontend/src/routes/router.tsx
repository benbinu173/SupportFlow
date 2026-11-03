import { Outlet, createBrowserRouter, type RouteObject } from 'react-router-dom'

import { AuthLayout } from '@/layouts/AuthLayout'
import { MarketingLayout } from '@/layouts/MarketingLayout'
import { HealthPage } from '@/pages/HealthPage'
import { HomePage } from '@/pages/HomePage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { TicketQueuePage } from '@/pages/tickets/TicketQueuePage'
import { LoginPage } from '@/pages/auth/LoginPage'
import { RegisterPage } from '@/pages/auth/RegisterPage'
import { KitPage } from '@/pages/dev/KitPage'
import { LandingPage } from '@/pages/marketing/LandingPage'
import { RouteError } from '@/routes/RouteError'
import { RedirectIfAuthenticated, RequireAuth, RequirePermission } from '@/routes/guards'
import { HOME_BY_ROLE } from '@/routes/navigation'
import { PERMISSIONS, type UserRole } from '@/features/auth/permissions'

/**
 * Development-only routes.
 *
 * Gated on `import.meta.env.DEV` rather than left in and hidden, because a
 * component kit reachable in production is a page with a fixture user in it.
 * Vite replaces the flag with `false` at build time, so the branch and the
 * imports behind it are both dropped from the shipped bundle.
 *
 * `/dev/health` is the scaffold's readiness probe. It lost its place at `/` when
 * the landing page took the root, and it is here rather than deleted because it
 * still has a job before F8: it is the one screen that shows, without anything
 * between it and the server, whether the API, the database and the object store
 * are actually up. F8 moves that panel into the admin screen and deletes this.
 */
const devRoutes: RouteObject[] = import.meta.env.DEV
  ? [
      { path: '/dev/kit', element: <KitPage /> },
      { path: '/dev/health', element: <HealthPage /> },
    ]
  : []

/**
 * The route table.
 *
 * The root entry is pathless: it renders nothing of its own and exists to carry
 * the error element over every route at once.
 *
 * Note what is *not* here. The four role homes are one component, because in F2
 * they are one honest placeholder — the permission matrix is what differs, and
 * that lives in the sidebar and the store rather than in a route. F7 and F9
 * split them apart when there is genuinely different content to show; adding
 * four near-identical routes now would be four places to update for no gain.
 */
export const router = createBrowserRouter([
  {
    element: <Outlet />,
    errorElement: <RouteError />,
    children: [
      {
        path: '/',
        element: <MarketingLayout />,
        children: [{ index: true, element: <LandingPage /> }],
      },

      {
        // Signing in while already signed in is a dead end, so the auth screens
        // bounce an authenticated visitor to their own home instead.
        element: <RedirectIfAuthenticated to={(role: UserRole) => HOME_BY_ROLE[role]} />,
        children: [
          {
            element: <AuthLayout />,
            children: [
              { path: '/login', element: <LoginPage /> },
              { path: '/register', element: <RegisterPage /> },
            ],
          },
        ],
      },

      {
        element: <RequireAuth />,
        children: [
          { path: '/dashboard', element: <HomePage /> },
          {
            element: <RequirePermission permission={PERMISSIONS.TICKET_LIST} />,
            children: [{ path: '/tickets', element: <TicketQueuePage /> }],
          },
          { path: '/portal', element: <HomePage /> },
        ],
      },

      ...devRoutes,

      {
        path: '*',
        element: <NotFoundPage />,
      },
    ],
  },
])
