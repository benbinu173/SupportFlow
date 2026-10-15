import { create } from 'zustand'

export type ToastVariant = 'info' | 'success' | 'warning' | 'danger'

export interface Toast {
  id: string
  title: string
  description?: string
  variant: ToastVariant
}

export interface ToastInput {
  title: string
  description?: string
  variant?: ToastVariant
  /** Milliseconds before it dismisses itself. `0` pins it until dismissed —
   *  for the rare toast that reports a state rather than an event. */
  duration?: number
}

/** A counter rather than `crypto.randomUUID`: deterministic ids make the tests
 *  readable, and nothing here is a security boundary. */
let nextId = 0

function createId(): string {
  nextId += 1
  return `toast-${nextId}`
}

/* Errors linger twice as long. A success is confirmation of something the user
   already expected; an error is news, and news that vanishes in five seconds
   while it is being read is a bug report waiting to happen. */
const DEFAULT_DURATION: Record<ToastVariant, number> = {
  info: 5000,
  success: 5000,
  warning: 7000,
  danger: 9000,
}

interface ToastState {
  toasts: Toast[]
  push: (input: ToastInput) => string
  dismiss: (id: string) => void
  clear: () => void
}

export const useToastStore = create<ToastState>()((set, get) => ({
  toasts: [],

  push: (input) => {
    const id = createId()
    const variant = input.variant ?? 'info'
    const duration = input.duration ?? DEFAULT_DURATION[variant]

    set((state) => ({
      toasts: [...state.toasts, { ...input, id, variant }],
    }))

    if (duration > 0) {
      setTimeout(() => {
        get().dismiss(id)
      }, duration)
    }

    return id
  },

  dismiss: (id) => {
    set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) }))
  },

  clear: () => {
    set({ toasts: [] })
  },
}))

/**
 * Imperative toast helpers, for the call sites that are not a component: an
 * axios interceptor, a mutation's `onError`, a WebSocket handler.
 *
 * Deliberately not a hook. `useToastStore.getState()` is callable from a plain
 * module, and a session-expiry handler has no component to hang a hook off.
 */
export const toast = {
  show: (input: ToastInput) => useToastStore.getState().push(input),
  success: (title: string, description?: string) =>
    useToastStore.getState().push({ title, variant: 'success', ...(description ? { description } : {}) }),
  info: (title: string, description?: string) =>
    useToastStore.getState().push({ title, variant: 'info', ...(description ? { description } : {}) }),
  warning: (title: string, description?: string) =>
    useToastStore.getState().push({ title, variant: 'warning', ...(description ? { description } : {}) }),
  error: (title: string, description?: string) =>
    useToastStore.getState().push({ title, variant: 'danger', ...(description ? { description } : {}) }),
  dismiss: (id: string) => useToastStore.getState().dismiss(id),
}
