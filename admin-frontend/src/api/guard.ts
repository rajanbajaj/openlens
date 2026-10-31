/**
 * Runtime guards validating the response/error envelope shape of
 * `POST /api/v1/search`. Structural checks only (no `any` leaks:
 * every unknown is narrowed before use).
 */

import type {
  AdminConfigDoc,
  AiCitation,
  CacheStatus,
  Diagnostics,
  ErrorEnvelope,
  FeedbackResponse,
  FrontierSettings,
  FrontierSettingsResponse,
  FrontierStatus,
  FrontierWorkersResponse,
  FrontierWorker,
  ProviderPrefs,
  QueryProvenance,
  SearchResponse,
  SearchResult,
  SuggestResponse,
} from './types.ts';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function isNumberRecord(value: unknown): value is Record<string, number> {
  return isRecord(value) && Object.values(value).every((item) => typeof item === 'number');
}

export function isSearchResult(value: unknown): value is SearchResult {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === 'string' &&
    typeof value.title === 'string' &&
    typeof value.snippet === 'string' &&
    typeof value.original_score === 'number' &&
    typeof value.final_score === 'number' &&
    (typeof value.url === 'string' || value.url === null) &&
    typeof value.provider === 'string' &&
    isStringArray(value.reasons) &&
    isStringArray(value.warnings) &&
    isRecord(value.metadata)
  );
}

function isStringRecord(value: unknown): value is Record<string, string> {
  return isRecord(value) && Object.values(value).every((item) => typeof item === 'string');
}

export function isQueryProvenance(value: unknown): value is QueryProvenance {
  if (!isRecord(value)) return false;
  return (
    typeof value.normalized === 'string' &&
    typeof value.truncated === 'boolean' &&
    typeof value.intent === 'string' &&
    typeof value.suggested_mode === 'string' &&
    typeof value.explicit_mode === 'string' &&
    typeof value.effective_mode === 'string' &&
    isStringArray(value.base_terms) &&
    isStringArray(value.added_terms) &&
    isStringRecord(value.provenance) &&
    isStringArray(value.reasons)
  );
}

function isOptionalNumberRecord(value: unknown): boolean {
  return value === undefined || isNumberRecord(value);
}

export function isDiagnostics(value: unknown): value is Diagnostics {
  if (!isRecord(value)) return false;
  return (
    typeof value.fallback_used === 'boolean' &&
    isNumberRecord(value.provider_latencies_ms) &&
    Array.isArray(value.plugin_executions) &&
    value.plugin_executions.every(isRecord) &&
    isStringArray(value.errors) &&
    isOptionalNumberRecord(value.provider_result_counts) &&
    isOptionalNumberRecord(value.provider_costs_estimated) &&
    (value.query === undefined || isQueryProvenance(value.query)) &&
    (value.request_id === undefined || typeof value.request_id === 'string') &&
    (value.cache === undefined || isCacheStatus(value.cache))
  );
}

export function isCacheStatus(value: unknown): value is CacheStatus {
  if (!isRecord(value)) return false;
  return value.status === undefined || typeof value.status === 'string';
}

/** One AI source-attribution entry (T22); structural check only. */
export function isAiCitation(value: unknown): value is AiCitation {
  if (!isRecord(value)) return false;
  return (
    (value.id === undefined || typeof value.id === 'string') &&
    (value.title === undefined || typeof value.title === 'string') &&
    (value.url === undefined || value.url === null || typeof value.url === 'string')
  );
}

export function isProviderPrefs(value: unknown): value is ProviderPrefs {
  if (!isRecord(value)) return false;
  return (
    isStringArray(value.include) &&
    isStringArray(value.exclude) &&
    (typeof value.primary === 'string' || value.primary === null)
  );
}

export function isSearchResponse(value: unknown): value is SearchResponse {
  if (!isRecord(value)) return false;
  return (
    typeof value.query === 'string' &&
    typeof value.mode === 'string' &&
    Array.isArray(value.results) &&
    value.results.every(isSearchResult) &&
    typeof value.total === 'number' &&
    isStringArray(value.applied_filters) &&
    isDiagnostics(value.diagnostics)
  );
}

/**
 * `GET /api/v1/suggest` response shape (`SuggestResponseSchema`).
 * Structural check only; note `isSearchResponse` deliberately ignores
 * assistance fields so a malformed `suggestions`/`spelling`/`related`
 * entry degrades via the readers below instead of failing the page.
 */
export function isSuggestResponse(value: unknown): value is SuggestResponse {
  if (!isRecord(value)) return false;
  return typeof value.q === 'string' && isStringArray(value.suggestions);
}

/** `POST /api/v1/feedback` acknowledgement shape. */
export function isFeedbackResponse(value: unknown): value is FeedbackResponse {
  if (!isRecord(value)) return false;
  return (
    typeof value.doc_id === 'string' &&
    typeof value.action === 'string' &&
    typeof value.count === 'number'
  );
}

/**
 * Assistance readers: extract the optional search-assistance fields
 * from an unknown search payload, returning the absent value
 * (`[]` / `null`) for missing or malformed input. They never throw,
 * so malformed assistance renders as absent instead of crashing the UI.
 */

/** `suggestions` completions, or `[]` when absent or malformed. */
export function readSuggestions(response: unknown): string[] {
  if (!isRecord(response)) return [];
  const suggestions = response['suggestions'];
  return suggestions === undefined || !isStringArray(suggestions) ? [] : suggestions;
}

/** `related` queries, or `[]` when absent or malformed. */
export function readRelated(response: unknown): string[] {
  if (!isRecord(response)) return [];
  const related = response['related'];
  return related === undefined || !isStringArray(related) ? [] : related;
}

/** `spelling` hint, or `null` when absent, null, blank, or malformed. */
export function readSpelling(response: unknown): string | null {
  if (!isRecord(response)) return null;
  const spelling = response['spelling'];
  return typeof spelling === 'string' && spelling !== '' ? spelling : null;
}

/** `next_offset`, or `null` when absent, null, or malformed. */
export function readNextOffset(response: unknown): number | null {
  if (!isRecord(response)) return null;
  const nextOffset = response['next_offset'];
  return typeof nextOffset === 'number' && Number.isInteger(nextOffset) && nextOffset >= 0
    ? nextOffset
    : null;
}

function isFrontierQueueEntry(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.url === 'string' &&
    typeof value.depth === 'number' &&
    typeof value.status === 'string'
  );
}

/** One worker entry in `GET /api/v1/frontier/workers` / status. */
export function isFrontierWorker(value: unknown): value is FrontierWorker {
  if (!isRecord(value)) return false;
  return (
    typeof value.worker_id === 'string' &&
    typeof value.crawl_id === 'string' &&
    typeof value.last_seen === 'number' &&
    typeof value.claimed === 'number' &&
    typeof value.status === 'string'
  );
}

/** `GET /api/v1/frontier/status` response shape. */
export function isFrontierStatus(value: unknown): value is FrontierStatus {
  if (!isRecord(value)) return false;
  if (typeof value.paused !== 'boolean') return false;
  if (!Array.isArray(value.queue) || !value.queue.every(isFrontierQueueEntry)) return false;
  if (
    value.workers !== undefined &&
    (!Array.isArray(value.workers) || !value.workers.every(isFrontierWorker))
  ) {
    return false;
  }
  if (typeof value.queue_depth !== 'number') return false;
  if (!isRecord(value.checkpoint)) return false;
  if (
    typeof value.checkpoint.phase !== 'string' ||
    typeof value.checkpoint.pages_attempted !== 'number' ||
    typeof value.checkpoint.pages_indexed !== 'number'
  ) {
    return false;
  }
  if (!Array.isArray(value.new_urls)) return false;
  return value.new_urls.every(
    (entry: unknown) =>
      isRecord(entry) && typeof entry.url === 'string' && typeof entry.status === 'string',
  );
}

/** `GET /api/v1/frontier/workers` response shape. */
export function isFrontierWorkersResponse(value: unknown): value is FrontierWorkersResponse {
  if (!isRecord(value)) return false;
  if (typeof value.worker_count !== 'number') return false;
  return Array.isArray(value.workers) && value.workers.every(isFrontierWorker);
}

/** `GET/PUT /api/v1/frontier/settings` response shape. */
export function isFrontierSettingsResponse(value: unknown): value is FrontierSettingsResponse {
  if (!isRecord(value)) return false;
  const settings: unknown = value.settings;
  if (!isRecord(settings)) return false;
  return (
    typeof settings.max_pages === 'number' &&
    typeof settings.max_queued_urls === 'number' &&
    typeof settings.crawl_delay_s === 'number' &&
    typeof settings.default_max_depth === 'number' &&
    typeof settings.respect_robots === 'boolean' &&
    typeof settings.user_agent === 'string'
  );
}

/** Live workers, or `[]` when absent or malformed. */
export function readFrontierWorkers(value: unknown): FrontierWorker[] {
  if (!isRecord(value) || !Array.isArray(value.workers)) return [];
  return value.workers.filter(isFrontierWorker);
}

/** Queue entries, or `[]` when absent or malformed. */
export function readFrontierQueue(value: unknown): FrontierStatus['queue'] {
  if (!isRecord(value) || !Array.isArray(value.queue)) return [];
  return value.queue.filter(isFrontierQueueEntry) as FrontierStatus['queue'];
}

/** New-URL entries, or `[]` when absent or malformed. */
export function readFrontierNewUrls(value: unknown): FrontierStatus['new_urls'] {
  if (!isRecord(value) || !Array.isArray(value.new_urls)) return [];
  return value.new_urls.filter(
    (entry: unknown): entry is FrontierStatus['new_urls'][number] =>
      isRecord(entry) && typeof entry.url === 'string' && typeof entry.status === 'string',
  );
}

/** Settings payload, or `null` when absent or malformed. */
export function readFrontierSettings(value: unknown): FrontierSettings | null {
  if (!isFrontierSettingsResponse(value)) return null;
  return value.settings;
}

/** `GET /api/v1/admin/config/{name}` response shape (versioned doc). */
export function isAdminConfigDoc(value: unknown): value is AdminConfigDoc {
  if (!isRecord(value)) return false;
  if (typeof value.doc !== 'string') return false;
  const body: unknown = value.body;
  if (!isRecord(body) && !Array.isArray(body)) return false;
  if (typeof value.version !== 'number' || !Number.isInteger(value.version)) return false;
  if (typeof value.updated_at !== 'string' && value.updated_at !== null) return false;
  return value.source === 'db' || value.source === 'file';
}

export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (!isRecord(value)) return false;
  if (!isRecord(value.error)) return false;
  return typeof value.error.code === 'string' && typeof value.error.message === 'string';
}
