import { create } from 'zustand'

export type ThemePreference = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

/**
 * Deliberately duplicated in the pre-paint script in `index.html`, which runs
 * before any module is parsed and therefore cannot import this. Two readers of
 * one key: rename it here and the flash of the wrong theme comes back.
 */
export const THEME_STORAGE_KEY = 'supportflow.theme'

const DARK_QUERY = '(prefers-color-scheme: dark)'

export function systemTheme(): ResolvedTheme {
  return window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light'
}

export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  return preference === 'system' ? systemTheme() : preference
}

export function readStoredPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY)

    if (stored === 'light' || stored === 'dark' || stored === 'system') {
      return stored
    }
  } catch {
    // Storage can throw rather than return null: Safari in private mode denies
    // it outright, and an embedded webview can deny it per-origin. A theme
    // preference is not worth failing a page load over.
  }

  return 'system'
}

function writeStoredPreference(preference: ThemePreference): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, preference)
  } catch {
    // As above. The theme still applies for this session; it just will not be
    // remembered.
  }
}

/**
 * The only thing that touches the DOM. `index.css` hangs the entire dark token
 * set off `[data-theme='dark']`, so this single attribute is the whole switch —
 * including `color-scheme`, which the stylesheet sets alongside it so native
 * controls and scrollbars follow without a second write here.
 */
export function applyTheme(theme: ResolvedTheme): void {
  document.documentElement.dataset.theme = theme
}

interface ThemeState {
  preference: ThemePreference
  resolved: ResolvedTheme
  setPreference: (preference: ThemePreference) => void
  syncWithSystem: (system: ResolvedTheme) => void
}

const initialPreference = readStoredPreference()

export const useThemeStore = create<ThemeState>()((set, get) => ({
  preference: initialPreference,
  resolved: resolveTheme(initialPreference),

  setPreference: (preference) => {
    writeStoredPreference(preference)

    // Resolved synchronously with the preference rather than in an effect, so
    // there is no render in which the two disagree — a frame of the old theme
    // is exactly the artefact switching themes is supposed to avoid.
    set({ preference, resolved: resolveTheme(preference) })
  },

  syncWithSystem: (system) => {
    // Only while the user is following the system. Once they have chosen a
    // theme explicitly, an OS switch at sunset must not overrule them.
    if (get().preference !== 'system') {
      return
    }

    set({ resolved: system })
  },
}))
