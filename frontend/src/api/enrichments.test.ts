import { describe, expect, it } from 'vitest';
import { isSearchResponse, isSearchResult } from './guard.ts';
import {
  resultAiCitations,
  resultAiDigest,
  resultAlsoFrom,
  resultCommerce,
  resultConfidence,
  resultSignals,
  resultStoryId,
  resultUnderstanding,
  summarizePluginExecution,
} from './enrichments.ts';

const RICH_METADATA: Record<string, unknown> = {
  signals: { trust: 0.55, relevance: 1, bogus: 'high', freshness: 0.5 },
  story_id: 'story-2',
  also_from: ['opensearch', 42],
  price: 199.99,
  currency: 'USD',
  location: 'Berlin',
  facilities: ['wifi', 'pool'],
  published: '2026-10-27',
  ai_summary: 'Two headphones compared.',
  ai_perspectives: ['Audiophiles prefer wired.'],
  ai_intent: 'shopping',
  ai_entities: ['headphones'],
  ai_model: 'rule-based',
};

describe('enrichment readers', () => {
  it('reads display-ordered finite signals, skipping malformed values', () => {
    expect(resultSignals(RICH_METADATA)).toEqual([
      ['relevance', 1],
      ['trust', 0.55],
      ['freshness', 0.5],
    ]);
  });

  it('reads confidence in range, null when absent or out of range', () => {
    expect(resultConfidence({ confidence: 0.83 })).toBe(0.83);
    expect(resultConfidence({})).toBeNull();
    expect(resultConfidence({ confidence: 'high' })).toBeNull();
    expect(resultConfidence({ confidence: 1.5 })).toBeNull();
  });

  it('reads story grouping, federation attribution, and commerce attributes', () => {
    expect(resultStoryId(RICH_METADATA)).toBe('story-2');
    expect(resultAlsoFrom(RICH_METADATA)).toEqual(['opensearch']);
    expect(resultCommerce(RICH_METADATA)).toEqual({
      price: 199.99,
      currency: 'USD',
      location: 'Berlin',
      facilities: ['wifi', 'pool'],
      published: '2026-10-27',
    });
  });

  it('reads the labelled AI digest and understanding, absent when disabled', () => {
    expect(resultAiDigest(RICH_METADATA)).toEqual({
      summary: 'Two headphones compared.',
      perspectives: ['Audiophiles prefer wired.'],
      model: 'rule-based',
      citations: [],
    });
    expect(resultUnderstanding(RICH_METADATA)).toEqual({
      intent: 'shopping',
      entities: ['headphones'],
      model: 'rule-based',
    });
    expect(resultAiDigest({})).toBeNull();
    expect(resultUnderstanding({})).toBeNull();
  });

  it('degrades gracefully on absent or malformed metadata', () => {
    expect(resultSignals({})).toEqual([]);
    expect(resultSignals({ signals: 'high' })).toEqual([]);
    expect(resultStoryId({ story_id: 7 })).toBeNull();
    expect(resultAlsoFrom({})).toEqual([]);
    expect(resultCommerce({ price: 'free', facilities: 'wifi' })).toEqual({
      price: null,
      currency: null,
      location: null,
      facilities: ['wifi'],
      published: null,
    });
  });

  it('parses AI citations, degrading url-only gaps to plain-text entries', () => {
    expect(
      resultAiCitations({
        ai_citations: [
          { id: 'r1', title: 'Acme Headphones', url: 'https://example.com/h' },
          { id: 'r2', title: 'Unlinked Review', url: null },
          { id: 'r3', title: 'Missing Url' },
        ],
      }),
    ).toEqual([
      { id: 'r1', title: 'Acme Headphones', url: 'https://example.com/h' },
      { id: 'r2', title: 'Unlinked Review', url: null },
      { id: 'r3', title: 'Missing Url', url: null },
    ]);
    expect(
      resultAiDigest({
        ai_summary: 'Two headphones compared.',
        ai_model: 'rule-based',
        ai_citations: [{ id: 'r1', title: 'Acme Headphones', url: 'https://example.com/h' }],
      })?.citations,
    ).toEqual([{ id: 'r1', title: 'Acme Headphones', url: 'https://example.com/h' }]);
  });

  it('drops malformed AI citations without crashing', () => {
    expect(resultAiCitations({})).toEqual([]);
    expect(resultAiCitations({ ai_citations: 'https://example.com/h' })).toEqual([]);
    expect(resultAiCitations({ ai_citations: null })).toEqual([]);
    expect(
      resultAiCitations({
        ai_citations: [
          null,
          42,
          'https://example.com/h',
          {},
          { id: 'no-title' },
          { title: 'No Id' },
          { id: '', title: 'Blank Id', url: 'https://example.com/blank' },
          { id: 'r9', title: '', url: 'https://example.com/blank' },
          { id: 'r1', title: 'Kept', url: 7 },
          { id: 'r2', title: 'Blank Url', url: '' },
        ],
      }),
    ).toEqual([
      { id: 'r1', title: 'Kept', url: null },
      { id: 'r2', title: 'Blank Url', url: null },
    ]);
  });

  it('reads fixtures that pass guard.ts validation end to end', () => {
    const result: unknown = {
      id: 'r1',
      title: 'Acme Headphones',
      snippet: 'Great sound',
      original_score: 0.9,
      final_score: 0.95,
      url: 'https://example.com/h',
      provider: 'mock',
      reasons: ['matches query'],
      warnings: [],
      metadata: RICH_METADATA,
    };
    expect(isSearchResult(result)).toBe(true);
    if (!isSearchResult(result)) return;
    expect(resultSignals(result.metadata)).toEqual([
      ['relevance', 1],
      ['trust', 0.55],
      ['freshness', 0.5],
    ]);
    expect(resultStoryId(result.metadata)).toBe('story-2');
    expect(resultAiDigest(result.metadata)?.model).toBe('rule-based');

    const envelope: unknown = {
      query: 'wireless headphones',
      mode: 'shopping',
      results: [result],
      total: 1,
      applied_filters: ['user-restrictions'],
      diagnostics: {
        fallback_used: false,
        provider_latencies_ms: { mock: 12.5 },
        plugin_executions: [],
        errors: [],
        cache: { status: 'miss' },
      },
    };
    expect(isSearchResponse(envelope)).toBe(true);
  });

  it('summarizes plugin executions with unknown-tolerant fallbacks', () => {
    expect(
      summarizePluginExecution({
        plugin: 'safety-filter',
        stage: 'filtering',
        status: 'ok',
        latency_ms: 0.4,
        error: null,
      }),
    ).toEqual({
      plugin: 'safety-filter',
      stage: 'filtering',
      status: 'ok',
      latencyMs: 0.4,
      error: null,
    });
    expect(summarizePluginExecution({})).toEqual({
      plugin: 'unknown',
      stage: 'unknown',
      status: 'unknown',
      latencyMs: null,
      error: null,
    });
  });
});
