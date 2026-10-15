import { afterEach, describe, expect, it, vi } from 'vitest'

/**
 * The shared client's configuration, which is the whole of this module.
 *
 * `baseURL` is composed from `import.meta.env.VITE_API_BASE_URL` at import time, so the two
 * branches are reached by re-importing the module with the variable stubbed, not by mutating
 * a property afterwards — mutating would test the assertion, not the composition.
 */
async function loadClient() {
  const { apiClient } = await import('./client')
  return apiClient
}

/**
 * Every case here pays for a *cold* dynamic import — `resetModules` in the teardown guarantees
 * it — and axios is not a small module graph. In isolation that is two seconds; with the rest
 * of the suite running in parallel it has been measured past six, which is what the default
 * five-second timeout was actually catching. The wait is module loading, not anything this
 * file asserts about the client, so the budget is raised to match what is being measured
 * rather than leaving a test that fails on a busy machine.
 */
const COLD_IMPORT_TIMEOUT = 20_000

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('apiClient', () => {
  it('prefixes the versioned API path onto the configured origin', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.com')

    const client = await loadClient()

    expect(client.defaults.baseURL).toBe('https://api.example.com/api/v1')
  }, COLD_IMPORT_TIMEOUT)

  it('stays same-origin when no base URL is configured', async () => {
    // The container build sets this to the empty string on purpose, so the SPA calls its own
    // origin and the reverse proxy decides where the API is. A leading `undefined` in the
    // path would be a 404 on every request.
    vi.stubEnv('VITE_API_BASE_URL', '')

    const client = await loadClient()

    expect(client.defaults.baseURL).toBe('/api/v1')
  }, COLD_IMPORT_TIMEOUT)

  it('sends the refresh cookie', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '')

    const client = await loadClient()

    // Without this the HttpOnly refresh cookie is not sent on a cross-origin request and
    // the session cannot be renewed.
    expect(client.defaults.withCredentials).toBe(true)
  }, COLD_IMPORT_TIMEOUT)

  it('gives up on a hung request rather than holding it open', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '')

    const client = await loadClient()

    expect(client.defaults.timeout).toBe(15_000)
  }, COLD_IMPORT_TIMEOUT)

  it('sends JSON by default', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '')

    const client = await loadClient()

    expect(client.defaults.headers['Content-Type']).toBe('application/json')
  }, COLD_IMPORT_TIMEOUT)
})
