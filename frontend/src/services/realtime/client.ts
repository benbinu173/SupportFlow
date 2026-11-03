import { refreshSession } from '@/services/api/session-refresh'
import { currentAccessToken } from '@/store/auth'

import { invalidateForEnvelope } from './invalidate'
import { isRealtimeEnvelope } from './protocol'
import type { QueryClient } from '@tanstack/react-query'

export type RealtimeStatus = 'inactive' | 'connecting' | 'connected' | 'reconnecting' | 'offline'
type StatusListener = (status: RealtimeStatus) => void

const MAX_RETRY_DELAY_MS = 30_000
const INITIAL_RETRY_DELAY_MS = 500

/** Build the socket address from the same API origin used by the HTTP client. */
export function realtimeUrl(apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? ''): string {
  const url = new URL(apiBaseUrl || window.location.origin, window.location.origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.pathname = '/ws'
  url.search = ''
  url.hash = ''
  return url.toString()
}

/** Owns one authenticated connection and its reconnect timers. */
export class RealtimeClient {
  private socket: WebSocket | null = null
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private stopped = true
  private retryAttempt = 0
  private refreshedForAuthFailure = false
  private hasConnected = false
  private status: RealtimeStatus = 'inactive'

  constructor(
    private readonly queryClient: QueryClient,
    private readonly onStatus: StatusListener,
    private readonly url = realtimeUrl(),
  ) {}

  start(): void {
    if (!this.stopped) return
    this.stopped = false
    this.connect()
  }

  stop(): void {
    this.stopped = true
    if (this.retryTimer !== null) clearTimeout(this.retryTimer)
    this.retryTimer = null
    const socket = this.socket
    this.socket = null
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'Session ended')
    this.setStatus('inactive')
  }

  private connect(): void {
    if (this.stopped) return
    this.setStatus(this.retryAttempt === 0 ? 'connecting' : 'reconnecting')

    let socket: WebSocket
    try {
      socket = new WebSocket(this.url)
    } catch {
      this.scheduleRetry()
      return
    }
    this.socket = socket

    socket.onopen = () => {
      if (this.stopped || this.socket !== socket) return
      const token = currentAccessToken()
      if (!token) {
        socket.close(1000, 'Session ended')
        return
      }
      socket.send(JSON.stringify({ type: 'auth', token }))
    }

    socket.onmessage = (message) => {
      if (this.stopped || this.socket !== socket || typeof message.data !== 'string') return
      let payload: unknown
      try {
        payload = JSON.parse(message.data)
      } catch {
        return
      }

      if (payload && typeof payload === 'object' && 'type' in payload && payload.type === 'authenticated') {
        if (this.hasConnected) void this.queryClient.invalidateQueries()
        this.hasConnected = true
        this.retryAttempt = 0
        this.refreshedForAuthFailure = false
        this.setStatus('connected')
        return
      }

      if (isRealtimeEnvelope(payload)) invalidateForEnvelope(this.queryClient, payload)
    }

    socket.onclose = (event) => {
      if (this.socket === socket) this.socket = null
      if (this.stopped) return

      if (event.code === 4401) {
        this.refreshAndRetry()
      } else if (event.code === 4403 || event.code === 4408 || event.code === 1000) {
        this.setStatus('offline')
      } else {
        // Includes 1013 (slow consumer) and transport failures with no close code.
        this.scheduleRetry()
      }
    }
  }

  private refreshAndRetry(): void {
    if (this.refreshedForAuthFailure) {
      this.setStatus('offline')
      return
    }
    this.refreshedForAuthFailure = true
    this.setStatus('reconnecting')
    void refreshSession()
      .then(() => {
        if (this.stopped) return
        this.retryAttempt = 0
        this.connect()
      })
      .catch(() => {
        // refreshSession owns the session transition and the user-facing message.
        this.setStatus('offline')
      })
  }

  private scheduleRetry(): void {
    if (this.stopped || this.retryTimer !== null) return
    this.setStatus('reconnecting')
    const exponential = Math.min(INITIAL_RETRY_DELAY_MS * 2 ** this.retryAttempt, MAX_RETRY_DELAY_MS)
    const jittered = Math.round(exponential * (0.75 + Math.random() * 0.5))
    this.retryAttempt += 1
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      this.connect()
    }, jittered)
  }

  private setStatus(status: RealtimeStatus): void {
    if (this.status === status) return
    this.status = status
    this.onStatus(status)
  }
}
