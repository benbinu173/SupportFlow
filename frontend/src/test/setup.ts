import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// Unmount between tests so DOM state cannot leak across cases.
afterEach(() => {
  cleanup()
})
