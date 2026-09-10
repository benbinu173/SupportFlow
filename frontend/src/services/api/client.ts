import axios from 'axios'

/**
 * Shared axios instance.
 *
 * `withCredentials` is required so the HttpOnly refresh cookie travels with
 * requests. Access-token attachment and refresh-on-401 retry are wired in the
 * auth phase; this file exists now so feature code has one place to import.
 */
export const apiClient = axios.create({
  baseURL: `${import.meta.env.VITE_API_BASE_URL ?? ''}/api/v1`,
  withCredentials: true,
  headers: { 'Content-Type': 'application/json' },
  timeout: 15_000,
})
