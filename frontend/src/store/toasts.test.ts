import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { toast, useToastStore } from './toasts'

beforeEach(() => {
  useToastStore.getState().clear()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('toast store', () => {
  it('pushes a toast and returns its id', () => {
    const id = useToastStore.getState().push({ title: 'Saved' })
    const { toasts } = useToastStore.getState()

    expect(toasts).toHaveLength(1)
    expect(toasts[0]?.id).toBe(id)
    expect(toasts[0]?.variant).toBe('info')
  })

  it('dismisses by id and leaves the rest alone', () => {
    const first = useToastStore.getState().push({ title: 'First' })
    useToastStore.getState().push({ title: 'Second' })

    useToastStore.getState().dismiss(first)

    const titles = useToastStore.getState().toasts.map((item) => item.title)

    expect(titles).toEqual(['Second'])
  })

  it('auto-dismisses after the variant default', () => {
    vi.useFakeTimers()

    useToastStore.getState().push({ title: 'Saved', variant: 'success' })

    vi.advanceTimersByTime(4_999)
    expect(useToastStore.getState().toasts).toHaveLength(1)

    vi.advanceTimersByTime(1)
    expect(useToastStore.getState().toasts).toHaveLength(0)
  })

  it('lets an error linger longer than a success', () => {
    // Errors are news; a success is confirmation of something already expected.
    // Both defaults are asserted so shortening one without the other fails
    // rather than passing as an accident of the shared five seconds.
    vi.useFakeTimers()

    useToastStore.getState().push({ title: 'Bad', variant: 'danger' })

    vi.advanceTimersByTime(5_000)
    expect(useToastStore.getState().toasts).toHaveLength(1)

    vi.advanceTimersByTime(4_000)
    expect(useToastStore.getState().toasts).toHaveLength(0)
  })

  it('pins a toast with duration 0', () => {
    vi.useFakeTimers()

    useToastStore.getState().push({ title: 'Working…', duration: 0 })

    vi.advanceTimersByTime(60_000)
    expect(useToastStore.getState().toasts).toHaveLength(1)
  })
})

describe('toast helpers', () => {
  it('maps each helper to its variant', () => {
    toast.success('a')
    toast.info('b')
    toast.warning('c')
    toast.error('d')

    expect(useToastStore.getState().toasts.map((item) => item.variant)).toEqual([
      'success',
      'info',
      'warning',
      // The helper is named `error` and the variant is `danger`, deliberately:
      // the call site describes what happened, the token describes what it
      // looks like.
      'danger',
    ])
  })

  it('is callable outside React', () => {
    // The reason these are not hooks: an axios interceptor and a WebSocket
    // handler both push toasts, and neither has a component to hang one off.
    expect(() => {
      toast.error('Session expired')
    }).not.toThrow()

    expect(useToastStore.getState().toasts[0]?.title).toBe('Session expired')
  })

  it('omits an empty description rather than storing an empty string', () => {
    toast.success('Done')

    expect(useToastStore.getState().toasts[0]).not.toHaveProperty('description')
  })
})
