/**
 * Runtime guards validating the response/error envelope shape of
 * `POST /api/v1/search`. Structural checks only (no `any` leaks:
 * every unknown is narrowed before use).
 */

import type {
  AgentTrace,
  AgentTraceStep,
  AiCitation,
  CacheStatus,
  Diagnostics,
  ErrorEnvelope,
  FeedbackResponse,
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

/**
 * Narrow one raw agentic-planning step: keep entries with at least
 * one usable string field, dropping malformed entries. Returns
 * `null` when the entry carries nothing renderable.
 */
function readAgentTraceStep(value: unknown): AgentTraceStep | null {
  if (!isRecord(value)) return null;
  const step: AgentTraceStep = {};
  if (typeof value['kind'] === 'string' && value['kind'] !== '') step.kind = value['kind'];
  if (typeof value['detail'] === 'string' && value['detail'] !== '') step.detail = value['detail'];
  return step.kind !== undefined || step.detail !== undefined ? step : null;
}

/**
 * Narrow a raw `metadata.agent_trace` value into a renderable
 * {@link AgentTrace}, or `null` when absent or malformed. Every
 * field is optional: partial traces render only the fields present,
 * and non-string list entries are dropped instead of crashing.
 */
function narrowAgentTrace(value: unknown): AgentTrace | null {
  if (!isRecord(value)) return null;
  const trace: AgentTrace = {};
  if (typeof value['rewritten_query'] === 'string' && value['rewritten_query'] !== '') {
    trace.rewritten_query = value['rewritten_query'];
  }
  if (isRecord(value['filters'])) trace.filters = value['filters'];
  if (Array.isArray(value['steps'])) {
    const steps = value['steps']
      .map(readAgentTraceStep)
      .filter((s): s is AgentTraceStep => s !== null);
    if (steps.length > 0) trace.steps = steps;
  }
  if (Array.isArray(value['fallbacks'])) {
    const fallbacks = value['fallbacks'].filter(
      (f): f is string => typeof f === 'string' && f !== '',
    );
    if (fallbacks.length > 0) trace.fallbacks = fallbacks;
  }
  if (
    typeof value['model_calls'] === 'number' &&
    Number.isInteger(value['model_calls']) &&
    value['model_calls'] >= 0
  ) {
    trace.model_calls = value['model_calls'];
  }
  return Object.keys(trace).length > 0 ? trace : null;
}

/**
 * `metadata.agent_trace` from a search payload, or `null` when the
 * backend omitted it (non-agent modes, older envelopes) or it is
 * malformed. Never throws, so a bad trace renders as absent.
 */
export function readAgentTrace(response: unknown): AgentTrace | null {
  if (!isRecord(response)) return null;
  const metadata = response['metadata'];
  if (!isRecord(metadata)) return null;
  return narrowAgentTrace(metadata['agent_trace']);
}

/** `next_offset`, or `null` when absent, null, or malformed. */
export function readNextOffset(response: unknown): number | null {
  if (!isRecord(response)) return null;
  const nextOffset = response['next_offset'];
  return typeof nextOffset === 'number' && Number.isInteger(nextOffset) && nextOffset >= 0
    ? nextOffset
    : null;
}

/**
 * URL-frontier shapes live in the admin app now: frontier guards and
 * readers were removed here with the dashboard. The backend
 * `/api/v1/frontier/*` routes and their `X-Admin-Token` mutation gate
 * are unchanged.
 */

export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (!isRecord(value)) return false;
  if (!isRecord(value.error)) return false;
  return typeof value.error.code === 'string' && typeof value.error.message === 'string';
}
