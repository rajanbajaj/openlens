/**
 * TypeScript mirror of the OpenLens HTTP contract in
 * `src/openlens/api/schemas.py` (`POST /api/v1/search`).
 *
 * Mirrored by hand, never imported from the backend: if `schemas.py`
 * changes, these types must be re-mirrored to match.
 */

/** Supported search modes (plan contract; backend validates non-blank). */
export type SearchMode = 'default' | 'child-safe' | 'news' | 'shopping' | 'travel';

/** POST /api/v1/search request body. */
export interface SearchRequest {
  /** 1-1000 chars, must not be blank. */
  query: string;
  /** Search mode; defaults to "default". */
  mode: string;
  /** 1-100; defaults to 10. */
  limit: number;
  /** >= 0; defaults to 0. */
  offset: number;
  /** Opaque filter mapping forwarded to the backend. */
  filters: Record<string, unknown>;
}

/** One normalized search hit on the wire. */
export interface SearchResult {
  id: string;
  title: string;
  snippet: string;
  original_score: number;
  final_score: number;
  url: string | null;
  provider: string;
  reasons: string[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

/** One plugin-execution record inside diagnostics (opaque entries). */
export type PluginExecution = Record<string, unknown>;

/** Per-request diagnostics embedded in the search response. */
export interface Diagnostics {
  fallback_used: boolean;
  provider_latencies_ms: Record<string, number>;
  plugin_executions: PluginExecution[];
  errors: string[];
}

/** POST /api/v1/search response body. */
export interface SearchResponse {
  query: string;
  mode: string;
  results: SearchResult[];
  total: number;
  applied_filters: string[];
  diagnostics: Diagnostics;
}

/** Machine-readable error code plus a human-readable message. */
export interface ErrorDetail {
  code: string;
  message: string;
}

/** Consistent error envelope used for every API failure. */
export interface ErrorEnvelope {
  error: ErrorDetail;
}

/** Either a successful search response or an error envelope. */
export type SearchApiPayload = SearchResponse | ErrorEnvelope;
