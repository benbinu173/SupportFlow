import { lazy } from 'react'

// These components render below the eager guards. Redirected or denied routes
// do not download their screen, and each explicit import is a split point.
export const AuthLayout = lazy(() => import('@/layouts/AuthLayout').then((module) => ({ default: module.AuthLayout })))
export const MarketingLayout = lazy(() => import('@/layouts/MarketingLayout').then((module) => ({ default: module.MarketingLayout })))
export const LandingPage = lazy(() => import('@/pages/marketing/LandingPage').then((module) => ({ default: module.LandingPage })))
export const LoginPage = lazy(() => import('@/pages/auth/LoginPage').then((module) => ({ default: module.LoginPage })))
export const RegisterPage = lazy(() => import('@/pages/auth/RegisterPage').then((module) => ({ default: module.RegisterPage })))
export const HomePage = lazy(() => import('@/pages/HomePage').then((module) => ({ default: module.HomePage })))
export const TicketQueuePage = lazy(() => import('@/pages/tickets/TicketQueuePage').then((module) => ({ default: module.TicketQueuePage })))
export const TicketDetailPage = lazy(() => import('@/features/tickets/TicketDetailPage').then((module) => ({ default: module.TicketDetailPage })))
export const PortalRequestsPage = lazy(() => import('@/pages/portal/PortalRequestsPage').then((module) => ({ default: module.PortalRequestsPage })))
export const NewRequestPage = lazy(() => import('@/pages/portal/NewRequestPage').then((module) => ({ default: module.NewRequestPage })))
export const CustomersPage = lazy(() => import('@/features/customers/CustomersPage').then((module) => ({ default: module.CustomersPage })))
export const CustomerDetailPage = lazy(() => import('@/features/customers/CustomerDetailPage').then((module) => ({ default: module.CustomerDetailPage })))
export const TeamPage = lazy(() => import('@/features/team/TeamPage').then((module) => ({ default: module.TeamPage })))
export const NotificationsPage = lazy(() => import('@/features/notifications/NotificationsPage').then((module) => ({ default: module.NotificationsPage })))
export const AnalyticsPage = lazy(() => import('@/features/analytics/AnalyticsPage').then((module) => ({ default: module.AnalyticsPage })))
export const SlaPage = lazy(() => import('@/features/sla/SlaPage').then((module) => ({ default: module.SlaPage })))
export const KnowledgePage = lazy(() => import('@/features/knowledge/KnowledgePage').then((module) => ({ default: module.KnowledgePage })))
export const AuditPage = lazy(() => import('@/features/audit/AuditPage').then((module) => ({ default: module.AuditPage })))
export const PreferencesPage = lazy(() => import('@/features/profile/PreferencesPage').then((module) => ({ default: module.PreferencesPage })))
