import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type ReactNode } from 'react'

import { RealtimeStatusContext } from '@/app/realtime-status'
import { RealtimeClient, type RealtimeStatus } from '@/services/realtime/client'
import { useSessionStore } from '@/store/auth'

export function RealtimeProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const sessionStatus = useSessionStore((state) => state.status)
  const [status, setStatus] = useState<RealtimeStatus>('inactive')

  useEffect(() => {
    if (sessionStatus !== 'authenticated') return

    const client = new RealtimeClient(queryClient, setStatus)
    client.start()
    return () => client.stop()
  }, [queryClient, sessionStatus])

  const visibleStatus = sessionStatus === 'authenticated' ? status : 'inactive'
  return <RealtimeStatusContext.Provider value={visibleStatus}>{children}</RealtimeStatusContext.Provider>
}
