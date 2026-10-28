import { describe, expect, it } from 'vitest';
import { resultAiDigest } from './enrichments.ts';
import { isCacheStatus, isDiagnostics, isErrorEnvelope, isSearchResponse } from './guard.ts';
import type { SearchResponse } from './types.ts';

/**
 * Fixture test: one real `POST /api/v1/search` response envelope
 * captured from the backend (FastAPI TestClient against the real
 * route + `SearchResponseSchema.from_core`, signal/rerank plugins
 * enabled, `filters: {"max_price": 250}`), kept parsing as the
 * contract-drift tripwire. Hand-editing this payload to match a
 * frontend change defeats its purpose: re-capture from the backend.
 */
const REAL_BACKEND_ENVELOPE: unknown = {
  query: 'wireless headphones',
  mode: 'shopping',
  results: [
    {
      id: 'h1',
      title: 'Acme Wireless Headphones',
      snippet: 'Great sound, noise cancelling headphones at a fair price',
      original_score: 2.039683712743124,
      final_score: 9.927183712743124,
      url: 'https://example.com/h1',
      provider: 'custom_index',
      reasons: [
        "matched 'wireless headphones' with provider score 2.040",
        'matched 2/2 query terms (+5.00)',
        "trust: unknown domain 'example.com'; https -> 0.55",
        'no publication date; freshness neutral 0.5',
        'no preferences requested; preference neutral 0.50',
        'no story group; perspective neutral 0.50',
        'commerce 1.00: within your budget (price 199.99 <= max 250)',
        "ranked with profile 'shopping'",
      ],
      warnings: [],
      metadata: {
        price: 199.99,
        currency: 'USD',
        signals: {
          relevance: 1.0,
          trust: 0.55,
          freshness: 0.5,
          preference: 0.5,
          diversity: 1.0,
          perspective: 0.5,
          commerce: 1.0,
        },
        recency_score: 0.5,
        facilities: [],
        location: null,
        perspective: 'unknown',
        published: null,
      },
    },
  ],
  total: 1,
  applied_filters: ['user-restrictions', 'safety-filter', 'privacy-filter'],
  diagnostics: {
    fallback_used: false,
    provider_latencies_ms: {
      custom_index: 0.24495800607837737,
    },
    provider_result_counts: {
      custom_index: 1,
    },
    provider_costs_estimated: {
      custom_index: 0.0,
    },
    plugin_executions: [
      {
        plugin: 'user-restrictions',
        stage: 'filtering',
        status: 'ok',
        latency_ms: 0.008125003660097718,
        error: null,
      },
      {
        plugin: 'safety-filter',
        stage: 'filtering',
        status: 'ok',
        latency_ms: 0.022249994799494743,
        error: null,
      },
      {
        plugin: 'privacy-filter',
        stage: 'filtering',
        status: 'ok',
        latency_ms: 0.025917019229382277,
        error: null,
      },
      {
        plugin: 'baseline-scorer',
        stage: 'scoring',
        status: 'ok',
        latency_ms: 0.04949999856762588,
        error: null,
      },
      {
        plugin: 'trust-scorer',
        stage: 'scoring',
        status: 'ok',
        latency_ms: 0.7165839779190719,
        error: null,
      },
      {
        plugin: 'freshness-scorer',
        stage: 'scoring',
        status: 'ok',
        latency_ms: 0.07958299829624593,
        error: null,
      },
      {
        plugin: 'preference-scorer',
        stage: 'scoring',
        status: 'ok',
        latency_ms: 0.002833025064319372,
        error: null,
      },
      {
        plugin: 'attribute-enricher',
        stage: 'enrichment',
        status: 'ok',
        latency_ms: 0.005709007382392883,
        error: null,
      },
      {
        plugin: 'diversity-reranker',
        stage: 'explanation',
        status: 'ok',
        latency_ms: 0.011333002476021647,
        error: null,
      },
      {
        plugin: 'news-reranker',
        stage: 'explanation',
        status: 'ok',
        latency_ms: 0.01591601176187396,
        error: null,
      },
      {
        plugin: 'commerce-reranker',
        stage: 'explanation',
        status: 'ok',
        latency_ms: 0.011333002476021647,
        error: null,
      },
    ],
    errors: [],
    query: {
      normalized: 'wireless headphones',
      truncated: false,
      intent: 'informational',
      suggested_mode: 'default',
      explicit_mode: 'shopping',
      effective_mode: 'shopping',
      base_terms: ['wireless', 'headphones'],
      added_terms: [],
      provenance: {},
      reasons: ["intent 'informational' by default (no keyword match)"],
    },
    request_id: '250406f7de9b47f99a1f0305d5d50e5f',
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
    expect(payload.results[0]?.reasons).toContain(
      'commerce 1.00: within your budget (price 199.99 <= max 250)',
    );
    expect(payload.applied_filters).toEqual([
      'user-restrictions',
      'safety-filter',
      'privacy-filter',
    ]);
    expect(payload.diagnostics.fallback_used).toBe(false);
    expect(payload.diagnostics.provider_latencies_ms).toEqual({
      custom_index: 0.24495800607837737,
    });
  });

  it('carries the T9–T15 diagnostics the enterprise UI renders', () => {
    expect(isSearchResponse(REAL_BACKEND_ENVELOPE)).toBe(true);
    if (!isSearchResponse(REAL_BACKEND_ENVELOPE)) return;
    const diagnostics = REAL_BACKEND_ENVELOPE.diagnostics;
    expect(diagnostics.request_id).toBe('250406f7de9b47f99a1f0305d5d50e5f');
    expect(diagnostics.provider_result_counts).toEqual({ custom_index: 1 });
    expect(diagnostics.provider_costs_estimated).toEqual({ custom_index: 0.0 });
    expect(diagnostics.query?.intent).toBe('informational');
    expect(diagnostics.query?.explicit_mode).toBe('shopping');
    expect(diagnostics.query?.effective_mode).toBe('shopping');
    const signals = REAL_BACKEND_ENVELOPE.results[0]?.metadata['signals'];
    expect(signals).toMatchObject({ trust: 0.55, commerce: 1.0 });
  });

  it('parses a real error envelope via the error guard, not the success guard', () => {
    expect(isErrorEnvelope(REAL_ERROR_ENVELOPE)).toBe(true);
    expect(isSearchResponse(REAL_ERROR_ENVELOPE)).toBe(false);
  });

  it('accepts cache hit/miss/error, empty, and absent cache diagnostics', () => {
    const base = REAL_BACKEND_ENVELOPE as unknown as Record<string, unknown>;
    const diagnostics = base['diagnostics'] as Record<string, unknown>;
    for (const cache of [
      { status: 'hit' },
      { status: 'miss' },
      { status: 'error' },
      {},
      undefined,
    ]) {
      expect(isDiagnostics({ ...diagnostics, cache })).toBe(true);
      expect(
        isSearchResponse(
          cache === undefined ? base : { ...base, diagnostics: { ...diagnostics, cache } },
        ),
      ).toBe(true);
    }
    // The rail badge reads `diagnostics.cache.status` only after this guard.
    const hit: unknown = { ...base, diagnostics: { ...diagnostics, cache: { status: 'hit' } } };
    expect(isSearchResponse(hit)).toBe(true);
    if (!isSearchResponse(hit)) return;
    expect(hit.diagnostics.cache?.status).toBe('hit');
  });

  it('validates the cache-status guard against malformed values', () => {
    expect(isCacheStatus({ status: 'hit' })).toBe(true);
    expect(isCacheStatus({ status: 'miss' })).toBe(true);
    expect(isCacheStatus({ status: 'error' })).toBe(true);
    expect(isCacheStatus({})).toBe(true);
    expect(isCacheStatus({ status: 42 })).toBe(false);
    expect(isCacheStatus('hit')).toBe(false);
    expect(isCacheStatus(null)).toBe(false);
    const base = REAL_BACKEND_ENVELOPE as unknown as Record<string, unknown>;
    const diagnostics = base['diagnostics'] as Record<string, unknown>;
    expect(isDiagnostics({ ...diagnostics, cache: { status: 42 } })).toBe(false);
    expect(isDiagnostics({ ...diagnostics, cache: 'hit' })).toBe(false);
  });

  it('rejects malformed payloads on both guards', () => {
    expect(isSearchResponse({ query: 'x' })).toBe(false);
    expect(isErrorEnvelope({ error: { code: 42 } })).toBe(false);
    expect(isSearchResponse(null)).toBe(false);
    expect(isErrorEnvelope(null)).toBe(false);
  });

  it('parses an envelope carrying AI metadata incl. ai_citations', () => {
    const envelope: unknown = {
      query: 'wireless headphones',
      mode: 'shopping',
      results: [
        {
          id: 'h1',
          title: 'Acme Wireless Headphones',
          snippet: 'Great sound',
          original_score: 2.0,
          final_score: 9.9,
          url: 'https://example.com/h1',
          provider: 'custom_index',
          reasons: ['matches query'],
          warnings: [],
          metadata: {
            ai_summary: 'Two models compared.',
            ai_perspectives: [],
            ai_model: 'rule-based',
            ai_citations: [
              { id: 'h1', title: 'Acme Wireless Headphones', url: 'https://example.com/h1' },
            ],
          },
        },
      ],
      total: 1,
      applied_filters: [],
      diagnostics: {
        fallback_used: false,
        provider_latencies_ms: {},
        plugin_executions: [],
        errors: [],
      },
    };
    expect(isSearchResponse(envelope)).toBe(true);
    if (!isSearchResponse(envelope)) return;
    const metadata = envelope.results[0]?.metadata as Record<string, unknown>;
    expect(resultAiDigest(metadata)?.citations).toEqual([
      { id: 'h1', title: 'Acme Wireless Headphones', url: 'https://example.com/h1' },
    ]);
  });
});
