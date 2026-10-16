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

/**
 * `IntersectionObserver` and `ResizeObserver`, neither of which jsdom
 * implements.
 *
 * The marketing surface enters with `whileInView`, and the `motion` library's
 * in-view feature constructs an observer the moment such a component mounts.
 * Without these, every test that renders the landing page throws rather than
 * failing an assertion.
 *
 * The intersection stub **never fires**. That is the honest behaviour for a
 * headless DOM with no layout: nothing is ever genuinely in the viewport, so a
 * reveal waiting on the callback keeps its initial state forever. Tests that
 * render the landing page therefore assert on what the page *says* rather than
 * on whether an element has finished animating into place, which is a claim
 * only a browser can settle.
 *
 * Both are assigned unconditionally rather than behind an `in` check. The check
 * would not compile: `lib.dom` declares both as always present on `window`, so
 * the body of `if (!('IntersectionObserver' in window))` is narrowed to `never`
 * and every assignment inside it is an error. The unconditional form is also
 * the truthful one — these files run only under Vitest, where jsdom is the
 * environment.
 */
class IntersectionObserverStub implements IntersectionObserver {
  readonly root: Element | Document | null = null
  readonly rootMargin: string = ''
  readonly thresholds: ReadonlyArray<number> = []
  readonly scrollMargin: string = ''

  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

class ResizeObserverStub implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.IntersectionObserver = IntersectionObserverStub
globalThis.ResizeObserver = ResizeObserverStub
