/**
 * Runtime guards validating the response/error envelope shape of
 * `POST /api/v1/search`. Structural checks only (no `any` leaks:
 * every unknown is narrowed before use).
 */

import type {
  Diagnostics,
  ErrorEnvelope,
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
  return (
    isRecord(value) &&
    Object.values(value).every((item) => typeof item === 'number')
  );
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

export function isDiagnostics(value: unknown): value is Diagnostics {
  if (!isRecord(value)) return false;
  return (
    typeof value.fallback_used === 'boolean' &&
    isNumberRecord(value.provider_latencies_ms) &&
    Array.isArray(value.plugin_executions) &&
    value.plugin_executions.every(isRecord) &&
    isStringArray(value.errors)
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
  return (
    typeof value.error.code === 'string' &&
    typeof value.error.message === 'string'
  );
}
