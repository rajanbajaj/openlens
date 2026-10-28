/**
 * Runtime guards validating the response/error envelope shape of
 * `POST /api/v1/search`. Structural checks only (no `any` leaks:
 * every unknown is narrowed before use).
 */

import type {
  AiCitation,
  CacheStatus,
  Diagnostics,
  ErrorEnvelope,
  ProviderPrefs,
  QueryProvenance,
  SearchResponse,
  SearchResult,
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

export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (!isRecord(value)) return false;
  if (!isRecord(value.error)) return false;
  return typeof value.error.code === 'string' && typeof value.error.message === 'string';
}
