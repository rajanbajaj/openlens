/**
 * TypeScript mirror of the OpenLens HTTP contract in
 * `src/openlens/api/schemas.py` (`POST /api/v1/search`).
 *
 * Mirrored by hand, never imported from the backend: if `schemas.py`
 * changes, these types must be re-mirrored to match.
 */

/** Supported search modes (plan contract; backend validates non-blank). */
export type SearchMode = 'default' | 'child-safe' | 'news' | 'shopping' | 'travel';

/** Per-query provider preferences for federated selection (T13). */
export interface ProviderPrefs {
  include: string[];
  exclude: string[];
  primary: string | null;
}

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
  /** Federated provider hints; omitted when the user sets none. */
  provider_prefs?: ProviderPrefs | null;
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

/**
 * Bounded query provenance entry (T9): normalization, intent, expansion,
 * and the mode suggestion. Explicit mode always wins over `suggested_mode`.
 */
export interface QueryProvenance {
  normalized: string;
  truncated: boolean;
  intent: string;
  suggested_mode: string;
  explicit_mode: string;
  effective_mode: string;
  base_terms: string[];
  added_terms: string[];
  provenance: Record<string, string>;
  reasons: string[];
}

/** Per-request diagnostics embedded in the search response. */
export interface Diagnostics {
  fallback_used: boolean;
  provider_latencies_ms: Record<string, number>;
  /** Result counts per provider (T13); absent on older envelopes. */
  provider_result_counts?: Record<string, number>;
  /** Estimated cost per provider (T13); absent on older envelopes. */
  provider_costs_estimated?: Record<string, number>;
  plugin_executions: PluginExecution[];
  errors: string[];
  /** Query provenance (T9); absent when the backend predates it. */
  query?: QueryProvenance;
  /** Opaque request id for log correlation (T15); empty when unset. */
  request_id?: string;
  /** Cache lookup outcome (T21: "hit" | "miss" | "error"); absent when uncached. */
  cache?: CacheStatus;
}

/** Cache lookup outcome reported in diagnostics (T21). */
export interface CacheStatus {
  /** "hit" | "miss" | "error"; absent on uncached responses (`cache: {}`). */
  status?: string;
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
