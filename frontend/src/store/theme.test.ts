import { afterEach, describe, expect, it } from 'vitest'

import {
  THEME_STORAGE_KEY,
  applyTheme,
  readStoredPreference,
  resolveTheme,
  useThemeStore,
} from './theme'

afterEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  useThemeStore.setState({ preference: 'system', resolved: 'light' })
})

describe('resolveTheme', () => {
  it('passes an explicit preference through', () => {
    expect(resolveTheme('light')).toBe('light')
    expect(resolveTheme('dark')).toBe('dark')
  })

  it('resolves `system` against the media query', () => {
    // The jsdom stub in src/test/setup.ts answers `matches: false`, so an
    // unmodified environment is a light-preferring OS.
    expect(resolveTheme('system')).toBe('light')
  })
})

describe('readStoredPreference', () => {
  it('reads a stored preference', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark')

    expect(readStoredPreference()).toBe('dark')
  })

  it('falls back to `system` when nothing is stored', () => {
    expect(readStoredPreference()).toBe('system')
  })

  it('ignores a stored value that is not a preference', () => {
    // A key collision, a half-written value from an older build, or a user
    // poking at localStorage must not produce an invalid theme.
    localStorage.setItem(THEME_STORAGE_KEY, 'solarized')

    expect(readStoredPreference()).toBe('system')
  })
})

describe('applyTheme', () => {
  it('writes the attribute the stylesheet keys off', () => {
    applyTheme('dark')

    expect(document.documentElement.dataset.theme).toBe('dark')
  })
})

describe('useThemeStore', () => {
  it('resolves synchronously on set, so no render sees the two disagree', () => {
    useThemeStore.getState().setPreference('dark')

    const { preference, resolved } = useThemeStore.getState()

    expect(preference).toBe('dark')
    expect(resolved).toBe('dark')
  })

  it('persists the preference', () => {
    useThemeStore.getState().setPreference('light')

    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
  })

  it('follows the system while the preference is `system`', () => {
    useThemeStore.setState({ preference: 'system', resolved: 'light' })

    useThemeStore.getState().syncWithSystem('dark')

    expect(useThemeStore.getState().resolved).toBe('dark')
  })

  it('ignores the system once the user has chosen a theme', () => {
    // Sunset must not overrule an explicit choice. This is the whole reason
    // `syncWithSystem` exists as something other than a plain setter.
    useThemeStore.setState({ preference: 'light', resolved: 'light' })

    useThemeStore.getState().syncWithSystem('dark')

    expect(useThemeStore.getState().resolved).toBe('light')
  })
})
