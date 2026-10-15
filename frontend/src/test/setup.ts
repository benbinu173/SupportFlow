import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// Unmount between tests so DOM state cannot leak across cases.
afterEach(() => {
  cleanup()
})

/**
 * `window.matchMedia`, which jsdom does not implement.
 *
 * The theme store reads it at module scope — the preference and the resolved
 * theme are computed when the module loads, not in an effect — so any test that
 * transitively imports `@/store/theme` needs this to exist before the import
 * runs, which is why it is here rather than in a test file.
 *
 * Answers `matches: false`, i.e. a light-preferring OS. That is the useful
 * default: a component that only looks right when the system is dark is a
 * component with a bug, and this makes the test suite say so.
 */
if (!window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string): MediaQueryList => {
    return {
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    } as unknown as MediaQueryList
  })
}
