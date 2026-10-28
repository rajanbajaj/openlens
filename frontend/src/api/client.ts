/**
 * Typed fetch wrapper for `POST {API_URL}/api/v1/search`.
 *
 * The API base URL comes only from `VITE_OPENLENS_API_URL`
 * (dev default `http://localhost:8000`). No hardcoded hosts,
 * no secrets or API keys anywhere in this module.
 */

import { isErrorEnvelope, isSearchResponse, isSuggestResponse } from './guard.ts';
import type { FeedbackRequest, ProviderPrefs, SearchRequest, SearchResponse } from './types.ts';

/**
 * Dev fallback when `VITE_OPENLENS_API_URL` is unset and the app runs
 * under `vite dev` (cross-origin by design). In a built bundle an unset
 * URL means same-origin (`''`), so requests go through the nginx `/api/`
 * proxy and never trigger a CORS preflight.
 */
export const DEFAULT_API_BASE_URL = 'http://localhost:8000';

/** Resolved API base URL (trailing slashes stripped). */
export const API_BASE_URL: string =
  (import.meta.env.VITE_OPENLENS_API_URL as string | undefined)?.trim().replace(/\/+$/, '') ||
  (import.meta.env.DEV ? DEFAULT_API_BASE_URL : '');

/** Backend default page size (`DEFAULT_LIMIT` in `schemas.py`). */
export const DEFAULT_LIMIT = 10;

/** Backend query ceiling (`MAX_QUERY_LENGTH` in `schemas.py`). */
export const MAX_QUERY_LENGTH = 1000;

/** Options accepted by {@link buildSearchBody}. */
export interface SearchOptions {
  query: string;
  mode?: string;
  limit?: number;
  offset?: number;
  filters?: Record<string, unknown>;
  providerPrefs?: ProviderPrefs | null;
}

/**
 * Validate a raw query string against the backend contract:
 * non-blank, 1–1000 chars. Returns an error message, or `null`
 * when the query is valid.
 */
export function validateQuery(query: string): string | null {
  if (!query.trim()) return 'Query must not be blank.';
  if (query.length > MAX_QUERY_LENGTH) {
    return `Query must be at most ${MAX_QUERY_LENGTH} characters.`;
  }
  return null;
}

/**
 * Shape a validated request body for `POST /api/v1/search`.
 * Applies backend defaults (`mode: "default"`, `limit: 10`,
 * `offset: 0`, `filters: {}`) for omitted fields.
 */
export function buildSearchBody(options: SearchOptions): SearchRequest {
  const body: SearchRequest = {
    query: options.query,
    mode: options.mode ?? 'default',
    limit: options.limit ?? DEFAULT_LIMIT,
    offset: options.offset ?? 0,
    filters: options.filters ?? {},
  };
  if (options.providerPrefs !== undefined && options.providerPrefs !== null) {
    body.provider_prefs = options.providerPrefs;
  }
  return body;
}

/** Machine-readable API failure raised by {@link searchLens}. */
export class SearchApiError extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'SearchApiError';
    this.code = code;
  }
}

type FetchImpl = typeof fetch;

/**
 * Issue `POST {API_URL}/api/v1/search` and return the parsed
 * `SearchResponse`. Throws {@link SearchApiError} for error
 * envelopes, malformed payloads, and transport failures.
 */
export async function searchLens(
  body: SearchRequest,
  fetchImpl: FetchImpl = fetch,
): Promise<SearchResponse> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new SearchApiError('invalid_response', 'Response was not valid JSON.');
  }

  if (isSearchResponse(payload)) return payload;
  if (isErrorEnvelope(payload)) {
    throw new SearchApiError(payload.error.code, payload.error.message);
  }
  throw new SearchApiError('invalid_response', 'Unexpected response shape.');
}

/**
 * Page count for a pre-pagination `total` at the given page size.
 * `total` is the full match count reported by the backend, not the
 * length of the current page. Returns 0 when there are no results.
 */
export function totalPages(total: number, limit: number): number {
  if (total <= 0 || limit <= 0) return 0;
  return Math.ceil(total / limit);
}

/** Offset of the first result on 1-based `page` at the given page size. */
export function offsetForPage(page: number, limit: number): number {
  return Math.max(0, (Math.max(1, Math.floor(page)) - 1) * limit);
}

/** 1-based page containing `offset` at the given page size. */
export function pageForOffset(offset: number, limit: number): number {
  if (limit <= 0) return 1;
  return Math.floor(Math.max(0, offset) / limit) + 1;
}

/** Debounce delay for as-you-type suggestions (plan contract). */
export const SUGGEST_DEBOUNCE_MS = 150;

type AnyArgs = unknown[];

/**
 * Debounce `fn`: restart the `delayMs` timer on every call and invoke
 * `fn` with the latest arguments once the timer elapses. The caller
 * owns cancellation via the returned `cancel`.
 */
export function debounce<Args extends AnyArgs>(
  fn: (...args: Args) => void,
  delayMs: number,
): ((...args: Args) => void) & { cancel: () => void } {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const debounced = (...args: Args): void => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, delayMs);
  };
  debounced.cancel = (): void => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };
  return debounced;
}

/** Options accepted by {@link fetchSuggestions}. */
export interface SuggestOptions {
  /** Prefix-completion cap (backend default 10, max 50). */
  limit?: number;
  /**
   * Caller-owned abort signal: the caller creates the
   * `AbortController`, aborts stale in-flight requests, and passes
   * `controller.signal` here. An abort resolves to `[]`, never throws.
   */
  signal?: AbortSignal;
}

/**
 * Issue `GET {API_URL}/api/v1/suggest` and return the completion
 * list. Never throws into rendering: blank queries skip the fetch,
 * and backend-absent (404/5xx), malformed, or aborted requests
 * resolve to `[]` so the UI degrades to direct search.
 */
export async function fetchSuggestions(
  query: string,
  options: SuggestOptions = {},
  fetchImpl: FetchImpl = fetch,
): Promise<string[]> {
  if (!query.trim()) return [];
  const params = new URLSearchParams({ q: query });
  if (options.limit !== undefined) params.set('limit', String(options.limit));
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/suggest?${params.toString()}`, {
      signal: options.signal,
    });
  } catch {
    return [];
  }
  if (!response.ok) return [];
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return [];
  }
  if (!isSuggestResponse(payload)) return [];
  return payload.suggestions;
}

/**
 * Fire-and-forget `POST {API_URL}/api/v1/feedback` click signal.
 * Sends only the anonymous (doc id, action) pair — no query text, no
 * user/session identifiers. Never rejects into rendering: transport
 * or backend failures resolve silently to `void`.
 */
export async function sendFeedback(
  docId: string,
  action: string,
  fetchImpl: FetchImpl = fetch,
): Promise<void> {
  if (!docId.trim() || !action.trim()) return;
  const body: FeedbackRequest = { doc_id: docId, action };
  try {
    await fetchImpl(`${API_BASE_URL}/api/v1/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    // Fire-and-forget: feedback must never break rendering.
  }
}

/**
 * Speculatively fetch 1-based `page` of a prior search `body` via the
 * existing offset math. Resolves to `null` (instead of throwing) when
 * the prefetch fails, so callers treat a miss as "not prefetched".
 */
export function prefetchPage(
  body: SearchRequest,
  page: number,
  fetchImpl: FetchImpl = fetch,
): Promise<SearchResponse | null> {
  const next: SearchRequest = { ...body, offset: offsetForPage(page, body.limit) };
  return searchLens(next, fetchImpl).then(
    (response) => response,
    () => null,
  );
}
