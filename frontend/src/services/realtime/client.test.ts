import { QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { refreshSession } from '@/services/api/session-refresh'
import { currentAccessToken } from '@/store/auth'

import { RealtimeClient, type RealtimeStatus } from './client'

vi.mock('@/services/api/session-refresh', () => ({ refreshSession: vi.fn() }))
vi.mock('@/store/auth', () => ({ currentAccessToken: vi.fn() }))

const AUTHENTICATION_TIMEOUT_MS = 15_000

class FakeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: FakeWebSocket[] = []

  readonly url: string
  readonly sent: string[] = []
  readonly closeCodes: number[] = []
  readyState: number = FakeWebSocket.CONNECTING
  onopen: ((event: Event) => unknown) | null = null
  onmessage: ((event: MessageEvent) => unknown) | null = null
  onclose: ((event: CloseEvent) => unknown) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  message(payload: unknown): void {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(payload) }))
  }

  send(data: string): void {
    this.sent.push(data)
  }

  close(code = 1000): void {
    this.readyState = FakeWebSocket.CLOSING
    this.closeCodes.push(code)
    // Model a proxy that never supplies the server's close frame.
  }

  finishClose(code: number): void {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close', { code }))
  }
}

function socketAt(index = 0): FakeWebSocket {
  const socket = FakeWebSocket.instances[index]
  if (!socket) throw new Error('Expected WebSocket ' + index + ' to exist')
  return socket
}

describe('realtime authentication deadlines', () => {
  let client: RealtimeClient
  let queryClient: QueryClient
  let statuses: RealtimeStatus[]

  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    vi.stubGlobal('WebSocket', FakeWebSocket)
    FakeWebSocket.instances = []
    vi.mocked(currentAccessToken).mockReturnValue('access-token')
    vi.mocked(refreshSession).mockImplementation(async () => {
      vi.mocked(currentAccessToken).mockReturnValue('fresh-token')
      return 'fresh-token'
    })
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    statuses = []
    client = new RealtimeClient(queryClient, (status) => statuses.push(status), 'wss://supportflow.example/ws')
  })

  afterEach(() => {
    client.stop()
    queryClient.clear()
    vi.unstubAllGlobals()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('stays connected when the server acknowledges before the deadline', async () => {
    client.start()
    const socket = socketAt()
    socket.open()
    expect(socket.sent).toEqual([JSON.stringify({ type: 'auth', token: 'access-token' })])
    expect(statuses.at(-1)).toBe('connecting')

    await vi.advanceTimersByTimeAsync(AUTHENTICATION_TIMEOUT_MS - 1)
    socket.message({ type: 'authenticated' })
    await vi.advanceTimersByTimeAsync(AUTHENTICATION_TIMEOUT_MS * 2)

    expect(statuses.at(-1)).toBe('connected')
    expect(refreshSession).not.toHaveBeenCalled()
    expect(socket.closeCodes).toEqual([])
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('refreshes a stalled connection and ignores its late close event', async () => {
    client.start()
    const stalled = socketAt()
    stalled.open()

    await vi.advanceTimersByTimeAsync(AUTHENTICATION_TIMEOUT_MS)

    expect(refreshSession).toHaveBeenCalledTimes(1)
    expect(stalled.closeCodes).toEqual([1000])
    expect(FakeWebSocket.instances).toHaveLength(2)
    const replacement = socketAt(1)
    replacement.open()
    expect(replacement.sent).toEqual([JSON.stringify({ type: 'auth', token: 'fresh-token' })])
    replacement.message({ type: 'authenticated' })
    stalled.finishClose(1000)

    expect(statuses.at(-1)).toBe('connected')
    await vi.advanceTimersByTimeAsync(AUTHENTICATION_TIMEOUT_MS * 2)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('also bounds a transport handshake that never opens', async () => {
    client.start()

    await vi.advanceTimersByTimeAsync(AUTHENTICATION_TIMEOUT_MS)

    expect(refreshSession).toHaveBeenCalledTimes(1)
    expect(socketAt().closeCodes).toEqual([1000])
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('goes offline after the refreshed connection also misses authentication', async () => {
    client.start()
    socketAt().open()
    await vi.advanceTimersByTimeAsync(AUTHENTICATION_TIMEOUT_MS)
    socketAt(1).open()

    await vi.advanceTimersByTimeAsync(AUTHENTICATION_TIMEOUT_MS * 3)

    expect(refreshSession).toHaveBeenCalledTimes(1)
    expect(statuses.at(-1)).toBe('offline')
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('cancels the deadline when the session stops', async () => {
    client.start()
    socketAt().open()
    client.stop()

    await vi.advanceTimersByTimeAsync(AUTHENTICATION_TIMEOUT_MS * 2)

    expect(statuses.at(-1)).toBe('inactive')
    expect(refreshSession).not.toHaveBeenCalled()
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it.each(['resolve', 'reject'] as const)('stays inactive when a stopped refresh later %ss', async (outcome) => {
    let resolveRefresh: (token: string) => void = () => undefined
    let rejectRefresh: (error: Error) => void = () => undefined
    vi.mocked(refreshSession).mockReturnValue(new Promise<string>((resolve, reject) => {
      resolveRefresh = resolve
      rejectRefresh = reject
    }))
    client.start()
    socketAt().finishClose(4401)
    client.stop()

    if (outcome === 'resolve') resolveRefresh('fresh-token')
    else rejectRefresh(new Error('Session expired'))
    await vi.advanceTimersByTimeAsync(0)

    expect(statuses.at(-1)).toBe('inactive')
    expect(FakeWebSocket.instances).toHaveLength(1)
  })
})
