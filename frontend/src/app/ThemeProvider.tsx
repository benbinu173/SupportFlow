import { useEffect, type ReactNode } from 'react'

import { applyTheme, useThemeStore } from '@/store/theme'

/**
 * Applies the resolved theme to the document and keeps it in step with the OS.
 *
 * The first paint is not this component's job — `index.html` resolves and sets
 * the attribute in a blocking script, because an effect only runs after React
 * has committed and a dark-mode user would see one light frame. This owns
 * everything after that: the OS changing, and the user choosing.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const resolved = useThemeStore((state) => state.resolved)
  const syncWithSystem = useThemeStore((state) => state.syncWithSystem)

  useEffect(() => {
    applyTheme(resolved)
  }, [resolved])

  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: dark)')

    const onSystemChange = () => {
      syncWithSystem(query.matches ? 'dark' : 'light')
    }

    // The OS can change in the window between the pre-paint script running and
    // this effect mounting. That script's answer is a snapshot, so re-read the
    // query once on mount rather than waiting for the next change event.
    onSystemChange()

    query.addEventListener('change', onSystemChange)

    return () => {
      query.removeEventListener('change', onSystemChange)
    }
  }, [syncWithSystem])

  return <>{children}</>
}
