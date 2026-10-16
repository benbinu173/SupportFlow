import axios from 'axios'

import { installInterceptors } from './interceptors'

/**
 * Shared axios instance for requests made on behalf of a signed-in user.
 *
 * `withCredentials` is required so the HttpOnly refresh cookie travels with
 * requests — without it neither `/auth/refresh` nor a cross-origin deployment
 * would authenticate at all.
 *
 * The interceptors attach the access token and recover from its expiry by
 * refreshing and replaying once. `public-client.ts` is the sibling without them,
 * for the requests that must not trigger a refresh: login, registration, and
 * refresh itself.
 */
export const apiClient = axios.create({
  baseURL: `${import.meta.env.VITE_API_BASE_URL ?? ''}/api/v1`,
  withCredentials: true,
  headers: { 'Content-Type': 'application/json' },
  timeout: 15_000,
})

installInterceptors(apiClient)
