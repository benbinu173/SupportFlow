import { createBrowserRouter } from 'react-router-dom'

import { HealthPage } from '@/pages/HealthPage'

/**
 * Route table. Role-scoped sections and auth guards are added in later phases;
 * for now a single page proves the shell renders and reaches the API.
 */
export const router = createBrowserRouter([
  {
    path: '/',
    element: <HealthPage />,
  },
])
