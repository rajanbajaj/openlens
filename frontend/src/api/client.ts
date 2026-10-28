/**
 * Typed fetch wrapper for `POST {API_URL}/api/v1/search`.
 *
 * The API base URL comes only from `VITE_OPENLENS_API_URL`
 * (dev default `http://localhost:8000`). No hardcoded hosts,
 * no secrets or API keys anywhere in this module.
 */

import { isErrorEnvelope, isSearchResponse } from './guard.ts';
import type { SearchRequest, SearchResponse } from './types.ts';

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
  return {
    query: options.query,
    mode: options.mode ?? 'default',
    limit: options.limit ?? DEFAULT_LIMIT,
    offset: options.offset ?? 0,
    filters: options.filters ?? {},
  };
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
