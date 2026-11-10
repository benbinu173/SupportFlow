import { lazy } from 'react'
import { createBrowserRouter, type RouteObject } from 'react-router-dom'

import { NotFoundPage } from '@/pages/NotFoundPage'
import { RouteError } from '@/routes/RouteError'
import { RouteOutlet } from '@/routes/RouteOutlet'
import {
  AnalyticsPage, AuditPage, AuthLayout, CustomerDetailPage, CustomersPage, HomePage,
  KnowledgePage, LandingPage, LoginPage, MarketingLayout, NewRequestPage, NotificationsPage,
  PortalRequestsPage, PreferencesPage, RegisterPage, SlaPage, TeamPage,
  TicketDetailPage, TicketQueuePage, ForgotPasswordPage, ResetPasswordPage,
} from '@/routes/lazy-pages'
import { RedirectIfAuthenticated, RequireAuth, RequirePermission, RequirePortalRole, RequireStaffRole } from '@/routes/guards'
import { HOME_BY_ROLE } from '@/routes/home'
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
      { path: '/dev/kit', Component: lazy(() => import('@/pages/dev/KitPage').then((module) => ({ default: module.KitPage }))) },
      { path: '/dev/health', Component: lazy(() => import('@/pages/HealthPage').then((module) => ({ default: module.HealthPage }))) },
    ]
  : []

/**
 * The route table.
 *
 * The pathless root provides the screen-loading boundary and error element
 * across the route table.
 *
 * Staff and portal routes share the same authentication boundary, then add the
 * narrowest role and permission guards required by each workflow. Keeping the
 * full route inventory explicit makes omissions visible in the route tests.
 */
export const router = createBrowserRouter([
  {
    element: <RouteOutlet />,
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
        // Recovery links also work when a browser already has an account session.
        // Successful recovery ends it and asks for a fresh sign-in.
        element: <AuthLayout />,
        children: [
          { path: '/forgot-password', element: <ForgotPasswordPage /> },
          { path: '/reset-password', element: <ResetPasswordPage /> },
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
