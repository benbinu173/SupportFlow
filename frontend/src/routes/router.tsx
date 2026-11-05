import { Outlet, createBrowserRouter, type RouteObject } from 'react-router-dom'

import { AuthLayout } from '@/layouts/AuthLayout'
import { MarketingLayout } from '@/layouts/MarketingLayout'
import { HealthPage } from '@/pages/HealthPage'
import { HomePage } from '@/pages/HomePage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { TicketQueuePage } from '@/pages/tickets/TicketQueuePage'
import { TicketDetailPage } from '@/features/tickets/TicketDetailPage'
import { PortalRequestsPage } from '@/pages/portal/PortalRequestsPage'
import { NewRequestPage } from '@/pages/portal/NewRequestPage'
import { CustomersPage } from '@/features/customers/CustomersPage'
import { CustomerDetailPage } from '@/features/customers/CustomerDetailPage'
import { TeamPage } from '@/features/team/TeamPage'
import { NotificationsPage } from '@/features/notifications/NotificationsPage'
import { AnalyticsPage } from '@/features/analytics/AnalyticsPage'
import { SlaPage } from '@/features/sla/SlaPage'
import { KnowledgePage } from '@/features/knowledge/KnowledgePage'
import { AuditPage } from '@/features/audit/AuditPage'
import { PreferencesPage } from '@/features/profile/PreferencesPage'
import { LoginPage } from '@/pages/auth/LoginPage'
import { RegisterPage } from '@/pages/auth/RegisterPage'
import { KitPage } from '@/pages/dev/KitPage'
import { LandingPage } from '@/pages/marketing/LandingPage'
import { RouteError } from '@/routes/RouteError'
import { RedirectIfAuthenticated, RequireAuth, RequirePermission, RequirePortalRole, RequireStaffRole } from '@/routes/guards'
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
 * `/dev/health` is a developer-facing readiness probe. It stays out of the
 * shipped application because its raw dependency status belongs in local
 * diagnostics, not in the staff or customer product navigation.
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
 * Staff and portal routes share the same authentication boundary, then add the
 * narrowest role and permission guards required by each workflow. Keeping the
 * full route inventory explicit makes omissions visible in the route tests.
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
          {
            element: <RequireStaffRole />,
            children: [
              { path: '/dashboard', element: <HomePage /> },
              {
                element: <RequirePermission permission={PERMISSIONS.ANALYTICS_OWN} />,
                children: [{ path: '/analytics', element: <AnalyticsPage /> }],
              },
              {
                element: <RequirePermission permission={PERMISSIONS.SLA_VIEW} />,
                children: [{ path: '/sla', element: <SlaPage /> }],
              },
              {
                element: <RequirePermission permission={PERMISSIONS.KB_LIST} />,
                children: [{ path: '/knowledge', element: <KnowledgePage /> }],
              },
              {
                element: <RequirePermission permission={PERMISSIONS.AUDIT_VIEW} />,
                children: [{ path: '/audit', element: <AuditPage /> }],
              },
              {
                element: <RequirePermission permission={PERMISSIONS.TICKET_LIST} />,
                children: [{ path: '/tickets', element: <TicketQueuePage /> }],
              },
              {
                element: <RequirePermission permission={PERMISSIONS.CUSTOMER_LIST} />,
                children: [
                  { path: '/customers', element: <CustomersPage /> },
                  { path: '/customers/:customerId', element: <CustomerDetailPage /> },
                ],
              },
              {
                element: <RequirePermission permission={PERMISSIONS.USER_LIST} />,
                children: [{ path: '/users', element: <TeamPage /> }],
              },
            ],
          },
          {
            element: <RequirePermission permission={PERMISSIONS.TICKET_VIEW} />,
            children: [{ path: '/tickets/:ticketId', element: <TicketDetailPage /> }],
          },
          {
            element: <RequirePortalRole />,
            children: [
              {
                element: <RequirePermission permission={PERMISSIONS.TICKET_LIST} />,
                children: [{ path: '/portal', element: <PortalRequestsPage /> }],
              },
              {
                element: <RequirePermission permission={PERMISSIONS.TICKET_CREATE} />,
                children: [{ path: '/portal/new', element: <NewRequestPage /> }],
              },
            ],
          },
          {
            element: <RequirePermission permission={PERMISSIONS.PROFILE_VIEW} />,
            children: [{ path: '/preferences', element: <PreferencesPage /> }],
          },
          {
            element: <RequirePermission permission={PERMISSIONS.NOTIFICATION_LIST} />,
            children: [{ path: '/notifications', element: <NotificationsPage /> }],
          },
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
