import { createContext, useContext } from 'react'

import type { RealtimeStatus } from '@/services/realtime/client'

export const RealtimeStatusContext = createContext<RealtimeStatus>('inactive')

/** Inactive is the safe default for isolated layouts and public routes. */
export function useRealtimeStatus(): RealtimeStatus {
  return useContext(RealtimeStatusContext)
}
