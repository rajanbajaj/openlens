import { describe, expect, it } from 'vitest';
import { isErrorEnvelope, isSearchResponse } from './guard.ts';
import type { SearchResponse } from './types.ts';

/**
 * Fixture test: one real `POST /api/v1/search` response envelope
 * captured from the backend (FastAPI TestClient against the real
 * route + `SearchResponseSchema.from_core`), kept parsing as the
 * contract-drift tripwire. Hand-editing this payload to match a
 * frontend change defeats its purpose: re-capture from the backend.
 */
const REAL_BACKEND_ENVELOPE: unknown = {
  query: 'wireless headphones',
  mode: 'shopping',
  results: [
    {
      id: 'r1',
      title: 'Acme Headphones',
      snippet: 'Great sound, noise cancelling',
      original_score: 0.9,
      final_score: 0.95,
      url: 'https://example.com/h',
      provider: 'mock',
      reasons: ['matches query'],
      warnings: ['demo warning'],
      metadata: {},
    },
  ],
  total: 1,
  applied_filters: ['exclude_sponsored'],
  diagnostics: {
    fallback_used: false,
    provider_latencies_ms: { mock: 12.5 },
    plugin_executions: [],
    errors: [],
  },
};

const REAL_ERROR_ENVELOPE: unknown = {
  error: { code: 'validation_error', message: 'query must not be blank' },
};

describe('real backend response envelope fixture', () => {
  it('parses as a SearchResponse with results, filters, and diagnostics', () => {
    expect(isSearchResponse(REAL_BACKEND_ENVELOPE)).toBe(true);
    if (!isSearchResponse(REAL_BACKEND_ENVELOPE)) return;
    const payload: SearchResponse = REAL_BACKEND_ENVELOPE;
    expect(payload.query).toBe('wireless headphones');
    expect(payload.mode).toBe('shopping');
    expect(payload.total).toBe(1);
    expect(payload.results).toHaveLength(1);
    expect(payload.results[0]?.reasons).toEqual(['matches query']);
    expect(payload.applied_filters).toEqual(['exclude_sponsored']);
    expect(payload.diagnostics.fallback_used).toBe(false);
    expect(payload.diagnostics.provider_latencies_ms).toEqual({ mock: 12.5 });
  });

  it('parses a real error envelope via the error guard, not the success guard', () => {
    expect(isErrorEnvelope(REAL_ERROR_ENVELOPE)).toBe(true);
    expect(isSearchResponse(REAL_ERROR_ENVELOPE)).toBe(false);
  });

  it('rejects malformed payloads on both guards', () => {
    expect(isSearchResponse({ query: 'x' })).toBe(false);
    expect(isErrorEnvelope({ error: { code: 42 } })).toBe(false);
    expect(isSearchResponse(null)).toBe(false);
    expect(isErrorEnvelope(null)).toBe(false);
  });
});
