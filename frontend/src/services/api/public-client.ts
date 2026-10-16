import axios from 'axios'

/**
 * The instance for requests made when there is no session yet.
 *
 * Login, registration, and refresh all go through here rather than through
 * `apiClient`, and the reason is a loop rather than a preference.
 *
 * `apiClient` carries a response interceptor that answers a 401 by refreshing
 * the session and retrying the original request. A login that fails is a 401 —
 * `INVALID_CREDENTIALS` — so routing login through `apiClient` would mean a
 * wrong password triggers a refresh attempt, which is itself one of the few
 * calls that can 401, and the two would bounce off each other. The interceptor
 * cannot simply exempt `/auth/login` by URL: `/auth/refresh` would still need
 * exempting, and every future addition to that list is a chance to forget.
 *
 * So the split is by credential rather than by path. `apiClient` is for requests
 * made *as* somebody and refreshes when its token goes stale. This one is for
 * requests that establish who somebody is, where a 401 is the answer rather than
 * a problem to route around.
 *
 * It still sets `withCredentials`, because `/auth/refresh` authenticates with
 * the HttpOnly cookie and a cross-origin request without it would send nothing.
 */
export const publicClient = axios.create({
  baseURL: `${import.meta.env.VITE_API_BASE_URL ?? ''}/api/v1`,
  withCredentials: true,
  headers: { 'Content-Type': 'application/json' },
  timeout: 15_000,
})
